"""Capability contracts, mutable offers, leases, fences and durable dispatch."""
import math
import time
from .api import op, ident, validate, schema
from .capability_usage import caller_usage, published_usage
from .store import Fault, uid, text, digest


def owner(ctx, provider):
    if ctx.permissions is not None and ctx.actor != provider:
        raise Fault('Provider identity mismatch', 403)


@op('cap.define', 'name:str inputs:obj outputs:obj version?:int description?:str synchronous?:bool usage?:obj')
def define(tx, ctx, name, inputs, outputs, version=1, description='', synchronous=False, usage=None):
    """Define an immutable executable schema; optionally publish revisable caller instructions and example_inputs."""
    key = ident(name) + ':' + str(version)
    if version < 1:
        raise Fault('Invalid contract version')
    record = {'id': key, 'name': name, 'version': version, 'inputs': schema(inputs),
              'outputs': schema(outputs), 'description': description, 'synchronous': synchronous}
    prior = tx.get('contracts', key) or tx.get('_retired_contracts', key)
    if prior and prior != record:
        raise Fault('Contract version is immutable', 409)
    if usage is not None:
        tx.put('_contract_usage', key, published_usage(record, usage))
    return tx.put('contracts', key, record)


@op('cap.offer', 'provider:str contract:str resource:str state?:str service?:str configuration?:obj capacity?:int usage?:obj')
def offer(tx, ctx, provider, contract, resource, state='ready', service='', configuration=None, capacity=None, usage=None):
    """Publish or revise an offer. Shared physical resources use the same resource ID."""
    owner(ctx, provider)
    if ctx.permissions is not None and provider in ('builtin', 'routine'):
        raise Fault('Reserved provider', 403)
    if ctx.permissions is not None and ('offer:' + contract not in ctx.permissions or 'resource:' + resource not in ctx.permissions):
        raise Fault('Offer or resource outside provider grant', 403)
    definition = tx.need('contracts', contract)
    policy = (configuration or {}).get('reservation_policy', {})
    if not isinstance(policy, dict):
        raise Fault('Invalid reservation policy')
    idle = policy.get('idle_seconds', 0)
    if isinstance(idle, bool) or not isinstance(idle, (int, float)) or not math.isfinite(idle) or idle < 0:
        raise Fault('Reservation idle seconds must be a nonnegative finite number')
    if 'release_after_result' in policy and not isinstance(policy['release_after_result'], bool):
        raise Fault('Reservation release_after_result must be boolean')
    if state not in ('ready', 'available_after_setup', 'offline'):
        raise Fault('Invalid offer state')
    if provider not in ('builtin', 'routine'):
        authority = tx.get('_enforcers', resource)
        if authority and authority != provider:
            raise Fault('Resource has another enforcement point', 409)
        tx.put('_enforcers', resource, provider)
    key = digest([provider, contract, resource])
    rec = {'id': key, 'provider': ident(provider), 'contract': contract, 'resource': ident(resource),
           'state': state, 'service': service, 'configuration': configuration or {}}
    if capacity is not None:
        if capacity < 0:
            raise Fault('Capacity must be 1 (exclusive), a positive slot count, or 0 (unbounded)')
        tx.put('_capacity', resource, capacity)
    if usage is not None:
        tx.put('_offer_usage', key, published_usage(definition, usage))
    tx.put('offers', key, rec)
    tx.emit('offer.updated', {'offer': key, 'state': state})
    return dict(rec, capacity=capacity_of(tx, rec))



@op('cap.delete', 'offer?:str contract?:str delete_contract?:bool allow_system?:bool')
def delete(tx, ctx, offer='', contract='', delete_contract=False, allow_system=False):
    """Delete an offer by ID. To separately delete an unused contract, supply contract and delete_contract=true."""
    if ctx.permissions is not None:
        raise Fault('Registry deletion requires operator access', 403)
    if not ((offer and not contract and not delete_contract) or
            (contract and delete_contract and not offer)):
        raise Fault('Identify exactly one offer, or one contract with delete_contract=true')
    kind, key = ('offers', offer) if offer else ('contracts', contract)
    record = tx.need(kind, key)
    cid = record['contract'] if offer else key
    if (cid.startswith('core.') or record.get('provider') in ('builtin', 'routine') or
            tx.get('_builtin_contracts', cid)) and not allow_system:
        raise Fault('System entry requires allow_system=true', 403)
    if contract and any(o['contract'] == cid for o in tx.rows('offers')):
        raise Fault('Delete associated offers first', 409)
    if any(l['contract'] == cid and l['state'] in ('queued', 'bound', 'fencing') and
           (not offer or l.get('offer') == offer or
            (l['state'] == 'queued' and l.get('requested_resource', '') in ('', record['resource'])))
           for l in tx.rows('leases')):
        raise Fault('Capability has active or queued leases', 409)
    if offer and any(a['offer'] == offer and a['state'] == 'pending' for a in tx.rows('activations')):
        raise Fault('Offer activation is pending', 409)
    if contract and any(node.get('contract') == cid for version in tx.rows('process_versions')
                        for node in version['graph']['nodes'].values()):
        raise Fault('A published workflow references this contract', 409)
    if offer and record.get('provider') in ('builtin', 'routine'):
        tx.put('_builtin_contracts', cid, True)
    if contract:
        tx.put('_retired_contracts', cid, record)
    usage_kind = '_offer_usage' if offer else '_contract_usage'
    if tx.get(usage_kind, key) is not None:
        tx.drop(usage_kind, key)
    tx.drop(kind, key)
    event = tx.emit('capability.deleted', {'kind': kind, 'id': key, 'contract': cid, 'actor': ctx.actor})
    return {'deleted': key, 'kind': kind, 'audit_event': event['id']}


@op('cap.discover', 'contract?:str', False)
def discover(tx, ctx, contract=''):
    """List capabilities with per-contract and provider-specific call examples, prerequisites and result paths."""
    definitions = {d['id']: d for d in tx.rows('contracts') if not contract or d['id'] == contract}
    guides = {key: tx.get('_contract_usage', key, {}) for key in definitions}
    contracts = [dict(definitions[key], usage=caller_usage(definitions[key], guides[key])) for key in sorted(definitions)]
    offers = []
    for off in tx.rows('offers'):
        key = off['contract']
        if key not in definitions:
            continue
        guide = dict(guides[key])
        override = tx.get('_offer_usage', off['id'], {})
        guide.update(override)
        if 'parameters' in override:
            guide['parameters'] = dict(guides[key].get('parameters', {}), **override['parameters'])
        offers.append(dict(off, capacity=capacity_of(tx, off),
                           usage=caller_usage(definitions[key], guide, off['resource'])))
    return {'contracts': contracts, 'offers': offers}


def admitted(tx, off):
    policy = tx.get('mission', 'current', {}).get('allocation', {})
    mode = policy.get(off['service'])
    accepted_modes = off.get('configuration', {}).get('modes', [])
    return off['state'] == 'ready' and (off['service'] not in policy or
                                        (mode is not None and (not accepted_modes or mode in accepted_modes)))


def capacity_of(tx, offer):
    return tx.get('_capacity', offer['resource'], 0 if offer['provider'] in ('builtin', 'routine') else 1)


def active_on(tx, resource):
    return [l for l in tx.rows('leases') if l['resource'] == resource and l['state'] in ('fencing', 'bound')]


def reservation_policy(tx, lease):
    offer = tx.get('offers', lease.get('offer', ''), {})
    return offer.get('configuration', {}).get('reservation_policy', {})


def touch_reservation(tx, lease):
    # Only opt-in, pre-dispatch reservations have an idle clock. This is never
    # an operation deadline: queued/claimed/uncertain work retains its fence.
    if lease['state'] in ('fencing', 'bound') and reservation_policy(tx, lease).get('idle_seconds', 0) > 0:
        tx.put('_reservation_activity', lease['id'], {'generation': lease['generation'], 'at': time.time()})


def recover_reservations(tx, ctx):
    now = time.time()
    actions = {}
    for action in tx.rows('actions'):
        actions.setdefault(action['lease'], []).append(action)
    recovered = 0
    for lease in tx.rows('leases'):
        if lease['state'] not in ('bound', 'fencing'):
            continue
        policy = reservation_policy(tx, lease)
        if not policy:
            continue
        work = [a for a in actions.get(lease['id'], []) if a['generation'] == lease['generation']]
        if any(a['state'] not in ('succeeded', 'failed') for a in work):
            continue
        # Combined submissions retain enough information to repair dispatch.
        if tx.get('_submission_leases', lease['id']) and not work:
            start_submission(tx, ctx, lease)
            continue
        reason = None
        if work and policy.get('release_after_result'):
            reason = 'settled_exchange'
        elif not work and policy.get('idle_seconds', 0) > 0:
            activity = tx.get('_reservation_activity', lease['id'])
            if not activity or activity.get('generation') != lease['generation']:
                # Older retained reservations get a full grace period on upgrade.
                touch_reservation(tx, lease)
            elif now - activity['at'] >= policy['idle_seconds']:
                reason = 'unused_reservation_idle'
        if reason and release(tx, lease):
            recovered += 1
            tx.emit('lease.recovered', {'lease': lease['id'], 'generation': lease['generation'], 'reason': reason})
    return recovered


def notify_bound(tx, ctx, lease):
    if lease.get('reply_to') and not lease.get('notified'):
        from .api import send, Context
        body = {k: lease[k] for k in ('id', 'task', 'contract', 'resource', 'generation', 'reply_to')}
        body.update(type='lease.available', state='bound')
        send(tx, Context(ctx.app, actor='controller', scope=ctx.scope), lease['reply_to'], body, lease['id'])
        tx.emit('lease.available', body)
        lease['notified'] = True


def bind(tx, ctx, request):
    for of in tx.rows('offers'):
        resource = of['resource']
        if of['contract'] != request['contract'] or not admitted(tx, of):
            continue
        if request['requested_resource'] and request['requested_resource'] != resource:
            continue
        if any(a['resource'] == resource and a['state'] == 'pending' for a in tx.rows('activations')):
            continue
        capacity = capacity_of(tx, of)
        if capacity:
            active = active_on(tx, resource)
            if len(active) >= capacity:
                continue
            # Slot zero uses the original guard and on-device fence key.
            held = {l['guard'] for l in active}
            for slot in range(len(active) + 1):
                guard_key = resource if slot == 0 else '!slot:%s:%d' % (resource, slot)
                if guard_key not in held:
                    break
        else:
            guard_key = '!shared:' + digest([resource, request['id']])
        guard = tx.get('guards', guard_key, {'generation': 0, 'holder': ''})
        if guard['holder']:
            continue
        generation = max(guard['generation'], request.get('generation', 0)) + 1
        lease = dict(request, offer=of['id'], provider=of['provider'], resource=resource,
                     generation=generation, guard=guard_key,
                     state='bound' if of['provider'] in ('builtin', 'routine') else 'fencing')
        tx.put('guards', guard_key, {'generation': generation, 'holder': lease['id']})
        if lease.get('queue_key'):
            tx.drop('_lease_queue', lease.pop('queue_key'))
        tx.emit('lease.acquired', {'lease': lease['id'], 'generation': generation})
        if lease['state'] == 'bound':
            notify_bound(tx, ctx, lease)
        tx.put('leases', lease['id'], lease)
        touch_reservation(tx, lease)
        start_submission(tx, ctx, lease)
        return lease


def promote(tx, ctx):
    # Keys follow committed event order. An unavailable resource does not
    # prevent an unrelated resource from serving its own oldest eligible waiter.
    granted = 0
    for key in tx.rows('_lease_queue'):
        if bind(tx, ctx, tx.need('leases', key)):
            granted += 1
            if granted == 32:
                return True  # Continue next tick within the replicated transaction budget.
    return False


@op('cap.acquire', 'task:str contract:str resource?:str enqueue?:bool reply_to?:str')
def acquire(tx, ctx, task, contract, resource='', enqueue=True, reply_to=''):
    """Acquire or queue. Use distinct task IDs for independent sessions. The worker fences automatically."""
    tx.need('contracts', contract)
    text(task, 'task')
    if reply_to:
        text(reply_to, 'reply_to')
    key = digest([task, contract, resource])
    batch_full = promote(tx, ctx)
    old = tx.get('leases', key, {})
    if old.get('state') == 'queued':
        return old
    if old.get('state') in ('bound', 'fencing'):
        if admitted(tx, tx.need('offers', old['offer'])) or not release(tx, old):
            return old
        batch_full = promote(tx, ctx)
    req = {'id': key, 'task': task, 'contract': contract, 'resource': resource,
           'requested_resource': resource, 'generation': old.get('generation', 0),
           'provider': '', 'reply_to': reply_to}
    lease = None if batch_full else bind(tx, ctx, req)
    if lease:
        return lease
    if not enqueue:
        raise Fault('No compatible available resource', 409)
    event = tx.emit('lease.queued', {'lease': key, 'task': task, 'contract': contract})
    req.update(state='queued', queue_key='%020d' % event['seq'],
               reply_to=reply_to or (ctx.actor if ctx.permissions is not None else task))
    tx.put('_lease_queue', req['queue_key'], key)
    return tx.put('leases', key, req)


@op('cap.lease', 'lease:str')
def lease_status(tx, ctx, lease):
    """Read the current lease, including queued requests, independently of its original receipt."""
    value = tx.need('leases', lease)
    touch_reservation(tx, value)
    return value


def executor(tx, ctx, provider):
    owner(ctx, provider)
    if ctx.permissions is None and tx.get('peers', provider):
        raise Fault('This operation must be signed by the enrolled provider. Callers use cap.status.', 403)


@op('cap.fenced', 'provider:str lease:str generation:int')
def fenced(tx, ctx, provider, lease, generation):
    """Acknowledge durable installation of the fence at the actual enforcement point."""
    executor(tx, ctx, provider)
    l = tx.need('leases', lease)
    guard = tx.need('guards', l['guard'])
    if (l['provider'], guard['holder'], guard['generation']) != (provider, lease, generation):
        raise Fault('Superseded fence', 409)
    l['state'] = 'bound'
    notify_bound(tx, ctx, l)
    return tx.put('leases', lease, l)


def release(tx, l):
    if l['state'] == 'released':
        return True
    if l['state'] == 'queued':
        tx.drop('_lease_queue', l.pop('queue_key'))
        l['state'] = 'released'
        tx.put('leases', l['id'], l)
        return True
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
    """Release a held lease after work settles, or cancel a queued request. Results remain available via cap.status."""
    l = tx.need('leases', lease)
    if ctx.permissions is not None:
        raise Fault('Lease release is controlled by the task owner', 403)
    if not release(tx, l):
        raise Fault('Reconcile outstanding operation before release', 409)
    promote(tx, ctx)
    return l


@op('cap.invoke', 'lease:str inputs:obj operation:str')
def dispatch(tx, ctx, lease, inputs, operation):
    """Submit once with a unique operation ID. Read cap.status with that ID for the result; the lease remains held until released."""
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
         'resource': l['resource'], 'generation': l['generation'], 'state': 'queued', 'inputs': inputs, 'guard': l['guard'],
         'status_query': {'operation': 'cap.status', 'arguments': {'operation': key}}}
    if l['provider'] == 'builtin' and l['contract'] == 'core.echo:1':
        a.update(state='succeeded', result={'value': inputs['value']})
    if l['provider'] == 'routine':
        from .process import new_run
        spec = tx.need('offers', l['offer'])['configuration']
        child = new_run(tx, spec['process'], inputs, parent='action:' + key, pin=spec['revision'])
        a.update(state='claimed', child=child['id'])
    tx.put('actions', key, a)
    tx.emit('operation.' + a['state'], {'id': key})
    return a


@op('provider.poll', 'provider:str', False)
def pending(tx, ctx, provider):
    """Provider work inbox. Completed actions leave this inbox and remain available via cap.status."""
    owner(ctx, provider)
    leases = [l for l in tx.rows('leases') if l['provider'] == provider and l['state'] == 'fencing']
    actions = [a for a in tx.rows('actions') if a['provider'] == provider and a['state'] in
               ('queued', 'claimed') and tx.need('leases', a['lease'])['state'] == 'bound']
    activations = [a for a in tx.rows('activations') if a['provider']
                   == provider and a['state'] == 'pending']
    first, rest, seen = [], [], set()
    for a in actions:
        guard = a.get('guard', a['resource'])
        (rest if guard in seen else first).append(a)
        seen.add(guard)
    return {'fences': leases, 'actions': (first + rest)[:64], 'activations': activations}


@op('provider.claim', 'provider:str operation:str generation:int')
def claim(tx, ctx, provider, operation, generation):
    executor(tx, ctx, provider)
    a = tx.need('actions', operation)
    l = tx.need('leases', a['lease'])
    g = tx.need('guards', l['guard'])
    if (a['provider'], l['state'], a['generation'], g['holder'], g['generation']) != (
            provider, 'bound', generation, a['lease'], generation):
        raise Fault('Stale resource generation', 409)
    if a['state'] == 'queued':
        if not admitted(tx, tx.need('offers', l['offer'])):
            raise Fault('Dispatch paused by availability or mission', 409)
        a['state'] = 'claimed'
        tx.put('actions', operation, a)
    return dict(a, output_schema=tx.need('contracts', a['contract'])['outputs'])


@op('provider.result', 'provider:str operation:str generation:int state:str result:obj')
def result(tx, ctx, provider, operation, generation, state, result):
    executor(tx, ctx, provider)
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
    finish_submission(tx, ctx, operation)
    if state in ('succeeded', 'failed') and reservation_policy(tx, tx.need('leases', a['lease'])).get('release_after_result'):
        if release(tx, tx.need('leases', a['lease'])):
            promote(tx, ctx)
    return a


@op('cap.status', 'operation:str', False)
def status(tx, ctx, operation):
    """Read action state and the provider result, including after the lease is released."""
    a = tx.need('actions', operation)
    owner(ctx, a['provider'])
    return a


@op('cap.leases', 'task?:str', False)
def leases(tx, ctx, task=''):
    return [l for l in tx.rows('leases') if not task or l['task'] == task]


def start_submission(tx, ctx, lease):
    operation = tx.get('_submission_leases', lease['id'])
    if not operation or lease['state'] not in ('bound', 'fencing'):
        return
    submission = tx.need('_submissions', operation)
    dispatch(tx, ctx, lease['id'], submission['inputs'], operation)
    finish_submission(tx, ctx, operation)


def finish_submission(tx, ctx, operation):
    submission = tx.get('_submissions', operation)
    action = tx.get('actions', operation)
    if submission and action and action['state'] in ('succeeded', 'failed'):
        release(tx, tx.need('leases', submission['lease']))
        if tx.get('_submission_active', operation):
            tx.drop('_submission_active', operation)


@op('cap.submit', 'contract:str resource:str inputs:obj operation:str')
def submit(tx, ctx, contract, resource, inputs, operation):
    """Durably acquire, fence, dispatch and release one command; safe across disconnects."""
    if ctx.permissions is not None:
        raise Fault('Submission requires task owner access', 403)
    ident(operation)
    validate(tx.need('contracts', contract)['inputs'], inputs)
    lease_id = digest([operation, contract, resource])
    record = dict(id=operation, lease=lease_id, contract=contract, resource=resource,
                  inputs=inputs, owner=ctx.actor)
    prior = tx.get('_submissions', operation)
    if prior and prior != record:
        raise Fault('Submission identity conflict', 409)
    action = tx.get('actions', operation)
    if action:
        if (action['lease'], action['contract'], action['inputs']) != (lease_id, contract, inputs):
            raise Fault('Operation identity conflict', 409)
        # Adopt an existing Android operation without replaying it or re-leasing.
        tx.put('_submissions', operation, record)
        finish_submission(tx, ctx, operation)
        return action
    tx.put('_submissions', operation, record)
    tx.put('_submission_leases', lease_id, operation)
    tx.put('_submission_active', operation, {'id': operation})
    lease = acquire(tx, ctx, operation, contract, resource)
    start_submission(tx, ctx, lease)
    return submission_status(tx, ctx, operation)


@op('cap.submission', 'operation:str', False)
def submission_status(tx, ctx, operation):
    """Read a combined request while queued, fencing, executing or completed."""
    submission = tx.need('_submissions', operation)
    if ctx.permissions is not None and submission['owner'] != ctx.actor:
        raise Fault('Other submission', 403)
    action = tx.get('actions', operation)
    if action:
        return action
    lease = tx.need('leases', submission['lease'])
    return dict(id=operation, lease=lease['id'], state=lease['state'])


def settle_submissions(tx, ctx):
    # Routine-backed commands finish outside provider.result. Tick releases only
    # completed combined requests; uncertain commands keep their original fence.
    for submission in tx.rows('_submission_active'):
        finish_submission(tx, ctx, submission['id'])
    if recover_reservations(tx, ctx):
        promote(tx, ctx)

