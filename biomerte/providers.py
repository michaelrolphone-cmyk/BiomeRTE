"""Capability contracts, mutable offers, leases, fences and durable dispatch."""
from .api import op, ident, validate, schema
from .store import Fault, uid, text, digest


def owner(ctx, provider):
    if ctx.permissions is not None and ctx.actor != provider:
        raise Fault('Provider identity mismatch', 403)


@op('cap.define', 'name:str inputs:obj outputs:obj version?:int description?:str synchronous?:bool')
def define(tx, ctx, name, inputs, outputs, version=1, description='', synchronous=False):
    key = ident(name) + ':' + str(version)
    if version < 1:
        raise Fault('Invalid contract version')
    record = {'id': key, 'name': name, 'version': version, 'inputs': schema(inputs),
              'outputs': schema(outputs), 'description': description, 'synchronous': synchronous}
    prior = tx.get('contracts', key)
    if prior and prior != record:
        raise Fault('Contract version is immutable', 409)
    return tx.put('contracts', key, record)


@op('cap.offer', 'provider:str contract:str resource:str state?:str service?:str configuration?:obj')
def offer(tx, ctx, provider, contract, resource, state='ready', service='', configuration=None):
    """Publish or revise an offer. Shared physical resources use the same resource ID."""
    owner(ctx, provider)
    if ctx.permissions is not None and provider in ('builtin','routine'):
        raise Fault('Reserved provider', 403)
    if ctx.permissions is not None and ('offer:'+contract not in ctx.permissions or 'resource:'+resource not in ctx.permissions):
        raise Fault('Offer or resource outside provider grant',403)
    tx.need('contracts', contract)
    if state not in ('ready', 'available_after_setup', 'offline'):
        raise Fault('Invalid offer state')
    if provider not in ('builtin','routine'):
        authority=tx.get('_enforcers',resource)
        if authority and authority!=provider:raise Fault('Resource has another enforcement point',409)
        tx.put('_enforcers',resource,provider)
    key = digest([provider, contract, resource])
    rec = {'id': key, 'provider': ident(provider), 'contract': contract, 'resource': ident(resource),
           'state': state, 'service': service, 'configuration': configuration or {}}
    tx.put('offers', key, rec)
    tx.emit('offer.updated', {'offer': key, 'state': state})
    return rec


@op('cap.discover', 'contract?:str', False)
def discover(tx, ctx, contract=''):
    return {'contracts': [r for r in tx.rows('contracts') if not contract or r['id'] == contract],
            'offers': [r for r in tx.rows('offers') if not contract or r['contract'] == contract]}


def admitted(tx, off):
    policy = tx.get('mission', 'current', {}).get('allocation', {})
    mode = policy.get(off['service'])
    accepted_modes = off.get('configuration',{}).get('modes',[])
    return off['state'] == 'ready' and (off['service'] not in policy or
            (mode is not None and (not accepted_modes or mode in accepted_modes)))


@op('cap.acquire', 'task:str contract:str resource?:str')
def acquire(tx, ctx, task, contract, resource=''):
    tx.need('contracts', contract)
    # A logical task handle can survive a safe rebind.
    key = digest([task, contract, resource])
    old = tx.get('leases', key)
    if old and old['state'] in ('bound', 'fencing'):
        if admitted(tx, tx.need('offers',old['offer'])) or not release(tx,old):
            return old
    for of in tx.rows('offers'):
        if of['contract'] != contract or not admitted(tx, of) or (resource and resource != of['resource']):
            continue
        guard_key = (of['resource']+':'+key) if of['provider'] in ('builtin','routine') else of['resource']
        guard = tx.get('guards', guard_key, {'generation': 0, 'holder': ''})
        if guard['holder']:
            continue
        gen = guard['generation'] + 1
        lease = {'id': key, 'task': task, 'contract': contract, 'offer': of['id'],
                 'provider': of['provider'], 'resource': of['resource'], 'generation': gen,
                 'guard': guard_key, 'state': 'bound' if of['provider'] in ('builtin','routine') else 'fencing'}
        tx.put('guards', guard_key, {'generation': gen, 'holder': key})
        tx.put('leases', key, lease)
        tx.emit('lease.acquired', {'lease': key, 'generation': gen})
        return lease
    raise Fault('No compatible available resource', 409)


@op('cap.fenced', 'provider:str lease:str generation:int')
def fenced(tx, ctx, provider, lease, generation):
    """Acknowledge durable installation of the fence at the actual enforcement point."""
    owner(ctx, provider)
    l = tx.need('leases', lease)
    guard = tx.need('guards', l['guard'])
    if (l['provider'], guard['holder'], guard['generation']) != (provider, lease, generation):
        raise Fault('Superseded fence', 409)
    l['state'] = 'bound'
    return tx.put('leases', lease, l)


def release(tx, l):
    if any(a['lease'] == l['id'] and a['state'] in ('queued', 'claimed', 'uncertain')
           for a in tx.rows('actions')):
        return False
    guard = tx.need('guards', l['guard'])
    if guard['holder'] == l['id'] and guard['generation'] == l['generation']:
        guard['holder'] = ''
        tx.put('guards', l['guard'], guard)
    l['state'] = 'released'
    tx.put('leases', l['id'], l)
    return True


def release_task(tx, task):
    for l in tx.rows('leases'):
        if l['task'] == task and l['state'] != 'released':
            release(tx, l)


@op('cap.release', 'lease:str')
def release_lease(tx, ctx, lease):
    l = tx.need('leases', lease)
    if ctx.permissions is not None:raise Fault('Lease release is controlled by the task owner',403)
    if not release(tx, l):
        raise Fault('Reconcile outstanding operation before release', 409)
    return l


@op('cap.invoke', 'lease:str inputs:obj operation:str')
def dispatch(tx, ctx, lease, inputs, operation):
    l = tx.need('leases', lease)
    key = ident(operation)
    prev = tx.get('actions', key)
    if prev:
        if prev['lease'] != lease or prev['inputs'] != inputs:
            raise Fault('Operation identity conflict', 409)
        return prev
    contract = tx.need('contracts', l['contract'])
    validate(contract['inputs'], inputs)
    if l['state'] not in ('bound', 'fencing') or not admitted(tx, tx.need('offers', l['offer'])):
        raise Fault('Lease unavailable', 409)
    a = {'id': key, 'contract': l['contract'], 'lease': lease, 'provider': l['provider'],
         'resource': l['resource'], 'generation': l['generation'], 'state': 'queued', 'inputs': inputs}
    if l['provider'] == 'builtin' and l['contract'] == 'core.echo:1':
        a.update(state='succeeded', result={'value': inputs['value']})
    if l['provider'] == 'routine':
        from .process import new_run
        spec = tx.need('offers',l['offer'])['configuration']
        child = new_run(tx,spec['process'],inputs,parent='action:'+key,pin=spec['revision'])
        a.update(state='claimed',child=child['id'])
    tx.put('actions', key, a)
    tx.emit('operation.' + a['state'], {'id': key})
    return a


@op('provider.poll', 'provider:str', False)
def pending(tx, ctx, provider):
    owner(ctx, provider)
    leases = [l for l in tx.rows('leases') if l['provider'] == provider and l['state'] == 'fencing']
    actions = [a for a in tx.rows('actions') if a['provider'] == provider and a['state'] in
               ('queued', 'claimed') and tx.need('leases', a['lease'])['state'] == 'bound']
    activations=[a for a in tx.rows('activations') if a['provider']==provider and a['state']=='pending']
    return {'fences': leases, 'actions': actions[:64], 'activations':activations}


@op('provider.claim', 'provider:str operation:str generation:int')
def claim(tx, ctx, provider, operation, generation):
    owner(ctx, provider)
    a = tx.need('actions', operation)
    l = tx.need('leases', a['lease'])
    g = tx.need('guards', a['resource'])
    if (a['provider'], l['state'], a['generation'], g['holder'], g['generation']) != (
            provider, 'bound', generation, a['lease'], generation):
        raise Fault('Stale resource generation', 409)
    if a['state'] == 'queued':
        if not admitted(tx, tx.need('offers', l['offer'])):
            raise Fault('Dispatch paused by availability or mission', 409)
        a['state'] = 'claimed'
        tx.put('actions', operation, a)
    return a


@op('provider.result', 'provider:str operation:str generation:int state:str result:obj')
def result(tx, ctx, provider, operation, generation, state, result):
    owner(ctx, provider)
    a = tx.need('actions', operation)
    if a['provider'] != provider or a['generation'] != generation:
        raise Fault('Result binding mismatch', 409)
    if state not in ('succeeded', 'failed', 'uncertain'):
        raise Fault('Unknown outcome')
    if a['state'] in ('succeeded', 'failed'):
        if a['state'] == state and a['result'] == result:
            return a
        raise Fault('Conflicting outcome', 409)
    if a['state'] not in ('claimed', 'uncertain'):
        raise Fault('Operation has not been claimed', 409)
    if state == 'succeeded':
        validate(tx.need('contracts', a['contract'])['outputs'], result)
    a.update(state=state, result=result)
    tx.put('actions', operation, a)
    tx.emit('operation.' + state, {'id': operation})
    return a


@op('cap.status', 'operation:str', False)
def status(tx, ctx, operation):
    a=tx.need('actions',operation)
    owner(ctx,a['provider'])
    return a


@op('cap.leases', 'task?:str', False)
def leases(tx, ctx, task=''):
    return [l for l in tx.rows('leases') if not task or l['task'] == task]
