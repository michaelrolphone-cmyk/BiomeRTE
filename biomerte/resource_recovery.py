"""Explicit resource custody, recovery observations and signed reconciliation."""
import time
from .api import op
from .store import Fault, digest
from . import lifecycle

SUPPORTED = ('host.terminal.run:1', 'docker.cli:1')


def protected(tx, lease):
    return bool(tx.get('offers', lease.get('offer', ''), {}).get('configuration', {}).get('execution_custody'))


def hold(tx, lease, reason, now):
    """Missing transport evidence cannot terminate an executing custodian."""
    changed = False
    active = [a for a in tx.matching('actions', ('claimed', 'uncertain'), lease=lease['id'])
              if a['generation'] == lease['generation']]
    if not active:
        return None
    for action in active:
        recovery = action.get('recovery', {})
        if action['state'] != 'uncertain' or recovery.get('reason') != reason:
            recovery = dict(reason=reason, requested_at=now, owner=action['provider'],
                            next_action='Reconnect runner and reconcile execution; do not replay or reassign',
                            last_confirmed_at=action.get('last_progress_at', action.get('claimed_at')),
                            required_evidence='Completed result or verified executor quiescence')
            action.update(state='uncertain', recovery=recovery)
            tx.put('actions', action['id'], action)
            tx.emit('operation.recovery_requested', {'id': action['id'], 'provider': action['provider'],
                                                   'lease': lease['id'], 'reason': reason})
            changed = True
    if not lease.get('recovery_required'):
        lease['recovery_required'] = True
        tx.put('leases', lease['id'], lease)
        changed = True
    return changed


@op('provider.custody', 'provider:str operation:str generation:int observation:obj')
def custody(tx, ctx, provider, operation, generation, observation):
    from .providers import executor
    executor(tx, ctx, provider)
    action = tx.need('actions', operation)
    identity = digest([action[k] for k in ('contract','inputs','resource','generation','lease')])
    if (action['provider'],action['generation']) != (provider,generation) or observation.get('signature') != identity:
        raise Fault('Custody identity mismatch',409)
    state = observation.get('state')
    if state not in ('starting','launching','running','finished','interrupted','uncertain','not_started'):
        raise Fault('Invalid custody state')
    now = time.time()
    allowed = ('state','runner_pid','adapter_pid','recovery_path','local_observed_at')
    action['custody'] = dict((k,observation[k]) for k in allowed if k in observation)
    action['custody']['observed_at'] = now
    if action['state'] in ('claimed','uncertain') and state in ('launching','running'):
        try:
            lifecycle.current(tx,action)
        except Fault:
            pass
        else:
            action['last_progress_at'] = now
            lease = tx.need('leases',action['lease'])
            cfg = lifecycle.policy(tx,lease)
            if cfg['lease_class'] == 'command':
                action['watch_until'] = now + cfg['watch_ttl_seconds']
                lease['watch_until'] = action['watch_until']
            lease.update(last_progress_at=now,missed_heartbeats=0)
            tx.put('leases',lease['id'],lease)
            if action.get('recovery'):
                action['recovery']['owner_reconnected_at'] = now
    tx.put('actions',operation,action)
    return {'operation':operation,'observed_at':now,'state':action['state']}


@op('provider.reconcile', 'provider:str operation:str generation:int state:str result:obj proof:obj')
def reconcile(tx,ctx,provider,operation,generation,state,result,proof):
    from .providers import executor
    from .providers import result as publish
    executor(tx,ctx,provider)
    action = tx.need('actions',operation)
    identity = digest([action[k] for k in ('contract','inputs','resource','generation','lease')])
    if (action['provider'],action['generation']) != (provider,generation):
        raise Fault('Reconciliation binding mismatch',409)
    if action['contract'] not in SUPPORTED or state not in ('succeeded','failed'):
        raise Fault('Unsupported execution reconciliation')
    if (proof.get('identity_digest') != identity or proof.get('quiescent') is not True or
            proof.get('method') not in ('runner_completion','process_identity','provider_restart_fence') or
            not isinstance(proof.get('evidence'),dict) or not proof['evidence']):
        raise Fault('Verified executor quiescence evidence is required',409)
    if action.get('execution_reconciliation'):
        if action['execution_reconciliation']['result_digest'] != digest(result):
            raise Fault('Conflicting execution reconciliation',409)
    else:
        action['execution_reconciliation'] = dict(proof,observed_at=time.time(),result_digest=digest(result))
        lease = tx.get('leases',action['lease'])
        guard = tx.get('guards',lease['guard']) if lease else None
        if action['state'] in ('claimed','uncertain') and (
                not guard or (guard['holder'],guard['generation']) != (action['lease'],generation)):
            action.update(state='abandoned',abandonment_result=action.get('result',{}))
        tx.put('actions',operation,action)
    return publish(tx,ctx,provider,operation,generation,state,result)


def compact(tx, lease, action, now):
    custody = action.get('custody',{})
    last = custody.get('observed_at',action.get('last_progress_at',action.get('claimed_at')))
    cfg = lifecycle.policy(tx,lease)
    age = None if last is None else max(0,now-last)
    unreachable = age is None or age > max(cfg['watch_ttl_seconds'],
        cfg['heartbeat_interval_seconds']*cfg['missed_heartbeats_limit'])
    state = ('stopping' if action.get('kill_requested') else
             'owner_unreachable' if unreachable else
             'reconciliation_required' if action['state']=='uncertain' else 'executing')
    return dict(operation=action['id'],lease=lease['id'],generation=action['generation'],
                provider=action['provider'],resource=action['resource'],guard=lease['guard'],
                state=state,command_state=action['state'],last_confirmed_at=last,
                observation_age_seconds=age,custody=custody,recovery=action.get('recovery',{}),
                next_action=('Inspect retained runner/process evidence and report verified quiescence'
                             if custody.get('state') in ('interrupted','uncertain') else
                             'Reconnect provider and reconcile original command ID' if unreachable else
                             'Await original completion or request verified stop'),
                reassignable=False)


@op('cap.resource.status','resource:str',False)
def status(tx,ctx,resource):
    from .providers import owner
    offers = [o for o in tx.rows('offers') if o['resource']==resource]
    for offer in offers:
        owner(ctx,offer['provider'])
    leases = [l for l in tx.matching('leases',('bound','fencing')) if l['resource']==resource]
    items = []
    now = time.time()
    for lease in leases:
        actions = tx.matching('actions',('claimed','uncertain'),lease=lease['id'])
        if actions:
            items.extend(compact(tx,lease,a,now) for a in actions)
        else:
            items.append(dict(lease=lease['id'],provider=lease.get('provider'),
                              generation=lease['generation'],guard=lease['guard'],
                              state='fencing' if lease['state']=='fencing' else 'reserved',
                              reassignable=False,next_action='Finish fence or release unused reservation'))
    return dict(resource=resource,state='held' if leases else 'free',owners=items,
                providers=[dict(provider=o['provider'],state=o['state'],
                                execution_custody=bool(o.get('configuration',{}).get('execution_custody')))
                           for o in offers],observed_at=now)


@op('cap.recovery.list','limit?:int after?:str',False)
def recovery_list(tx,ctx,limit=32,after=''):
    from .providers import owner
    if type(limit) is not int or not 1<=limit<=64:
        raise Fault('Invalid recovery page size')
    rows = sorted([a for a in tx.matching('actions',('claimed','uncertain')) if a['id']>after],
                  key=lambda a:a['id'])
    items=[]
    for action in rows[:limit]:
        owner(ctx,action['provider'])
        lease=tx.get('leases',action['lease'])
        if lease and lease['state'] in ('bound','fencing'):
            items.append(compact(tx,lease,action,time.time()))
        else:
            items.append(dict(operation=action['id'],provider=action['provider'],
                              resource=action['resource'],state='historical_uncertainty',
                              command_state=action['state'],reassignable=False,
                              next_action='Reconcile retained executor evidence; no active lease found'))
    return dict(items=items,next=rows[limit-1]['id'] if len(rows)>limit else None,
                observed_at=time.time())
