"""Durable deployment admission holds; reservation identity survives replacement.

A hold is separate from an execution fence. Started work keeps its original
custodian until it settles. Undispatched work is parked and refenced on resume.
"""
import time
from .api import op, ident
from .store import Fault, uid, digest
from . import lifecycle, placement

ACTIVE_LEASES = ('queued', 'fencing', 'bound', 'held')


def control(ctx):
    if ctx.permissions is not None:
        raise Fault('Redeployment control requires operator access', 403)


def current(tx, resource):
    key = tx.get('_redeploy_resources', resource)
    return tx.get('_redeployments', key) if key else None


def allowed(tx, offer, maintenance=''):
    hold = current(tx, offer['resource'])
    if hold:
        return maintenance == hold['id'] and hold['state'] != 'failed'
    return not maintenance


def selected(hold, offer):
    selector = hold['selector']
    if 'provider' in selector:
        return offer['provider'] == selector['provider']
    if 'service' in selector:
        return offer.get('service') == selector['service']
    return offer['resource'] in selector['resources']


def register_offer(tx, ctx, offer):
    """Provider/service holds also cover resources registered during rollout."""
    for hold in tx.rows('_redeployments'):
        if hold['state'] == 'completed' or not selected(hold, offer):
            continue
        other = tx.get('_redeploy_resources', offer['resource'])
        if other and other != hold['id']:
            raise Fault('Resource is already being redeployed', 409)
        if offer['resource'] not in hold['resources']:
            hold['resources'].append(offer['resource'])
            hold['resources'].sort()
            tx.put('_redeployments', hold['id'], hold)
        tx.put('_redeploy_resources', offer['resource'], hold['id'])


def target(tx, lease):
    if lease.get('maintenance_deployment'):
        return None
    for resource in (lease.get('resource'), lease.get('requested_resource')):
        hold = current(tx, resource) if resource else None
        if hold:
            return hold
    if lease.get('state') not in ('queued', 'held'):
        return None
    candidates = [o for o in tx.rows('offers') if o['contract'] == lease['contract']
                  and placement.matches(o, lease.get('requested_resource', ''), lease.get('placement'))]
    owners = {tx.get('_redeploy_resources', o['resource']) for o in candidates}
    if len(owners) == 1 and None not in owners:
        return tx.need('_redeployments', next(iter(owners)))
    return None


def live_actions(tx, lease):
    return [a for a in tx.matching('actions', ('claimed', 'uncertain'), lease=lease['id'])
            if a['generation'] == lease['generation']]


def park_action(tx, action, hold):
    if action['state'] != 'queued':
        return
    action.update(state='held', redeployment=hold['id'], held_at=time.time())
    tx.put('actions', action['id'], action)
    tx.emit('operation.held', {'id': action['id'], 'redeployment': hold['id']})


def park(tx, ctx, lease, hold):
    if lease.get('state') == 'held':
        return lease
    for action in tx.matching('actions', ('queued',), lease=lease['id']):
        park_action(tx, action, hold)
    if live_actions(tx, lease):
        return lease
    now = time.time()
    previous = lease['state']
    if previous in ('bound', 'fencing'):
        guard = tx.get('guards', lease.get('guard', ''))
        if guard and (guard['holder'], guard['generation']) == (lease['id'], lease['generation']):
            guard.update(holder='', generation=guard['generation'] + 1, released_at=now)
            tx.put('guards', lease['guard'], guard)
            tx.put('_fence_generations', lease['resource'],
                   max(guard['generation'], tx.get('_fence_generations', lease['resource'], 0)))
        activity = tx.get('_reservation_activity', lease['id'])
        idle = lifecycle.policy(tx, lease)['reservation_policy'].get('idle_seconds', 0)
        if idle and activity and activity.get('generation') == lease['generation']:
            lease['held_idle_remaining'] = max(0, idle - max(0, now - activity['at']))
    lease.update(state='held', held_from=previous, redeployment=hold['id'], held_at=now)
    lease.pop('expires_at', None)
    lease.pop('notified', None)
    tx.put('leases', lease['id'], lease)
    tx.emit('lease.held', {'lease': lease['id'], 'redeployment': hold['id']})
    return lease


def hold_request(tx, ctx, lease):
    hold = target(tx, lease)
    return park(tx, ctx, lease, hold) if hold else lease


def pause_streams(tx, hold):
    now = time.time()
    for grant in tx.matching('_stream_grants', ('open',)):
        if grant['resource'] not in hold['resources']:
            continue
        if grant['expires_at'] <= now:
            grant.update(state='closed', expires_at=now)
            tx.put('_stream_grants', grant['id'], grant)
            continue
        grant.update(state='held', redeployment=hold['id'], held_at=now,
                     held_remaining=max(0, grant['expires_at'] - now))
        # The router rejects held authority on its next validation. Transport
        # closure alone does not prove the device-owning adapter has stopped.
        grant['redeploy_quiescence_required'] = True
        grant['expires_at'] = None
        tx.put('_stream_grants', grant['id'], grant)
        tx.emit('stream.held', {'session': grant['id'], 'redeployment': hold['id']})


def reconcile(tx, ctx, id=''):
    holds = [tx.need('_redeployments', id)] if id else [
        h for h in tx.rows('_redeployments') if h['state'] != 'completed']
    if not holds:
        return
    for hold in holds:
        if hold['state'] != 'completed':
            pause_streams(tx, hold)
    for lease in tx.matching('leases', ACTIVE_LEASES):
        hold = target(tx, lease)
        if hold and (not id or hold['id'] == id):
            park(tx, ctx, lease, hold)


def snapshot(tx, hold):
    if hold['state'] == 'completed':
        return dict(hold, ready_to_deploy=False, held_leases=[], held_operations=[], held_streams=[], blockers=[])
    resources = set(hold['resources'])
    leases = [l for l in tx.matching('leases', ACTIVE_LEASES)
              if l.get('resource') in resources or l.get('redeployment') == hold['id']]
    blockers = []
    for lease in leases:
        for action in live_actions(tx, lease):
            blockers.append(dict(kind='operation', operation=action['id'],
                lease=lease['id'], resource=action['resource'], provider=action['provider'],
                state=action['state'], generation=action['generation']))
        if lease.get('maintenance_deployment') == hold['id']:
            pending = tx.matching('actions', ('queued', 'held'), lease=lease['id'])
            if pending:
                blockers.extend(dict(kind='maintenance', operation=a['id'], lease=lease['id'],
                                     resource=lease['resource'], state=a['state']) for a in pending)
    for activation in tx.matching('activations', ('pending',)):
        if activation['resource'] in resources:
            blockers.append(dict(kind='activation', activation=activation['id'],
                                 resource=activation['resource'], state='pending'))
    streams = [g for g in tx.matching('_stream_grants', ('held',))
               if g.get('redeployment') == hold['id']]
    for grant in streams:
        if grant.get('redeploy_quiescence_required') and not grant.get('redeploy_quiescence'):
            blockers.append(dict(kind='stream', session=grant['id'], resource=grant['resource'],
                state='quiescence_required',
                next_action='Stop the control stream at its enforcement point and record quiescence'))
    completed = hold['state'] == 'completed'
    state = hold['state'] if hold['state'] in ('failed', 'completed') else (
        'draining' if blockers else 'redeploying')
    return dict(hold, state=state, ready_to_deploy=state not in ('failed', 'completed') and not blockers,
        held_leases=[l['id'] for l in leases if l['state'] == 'held'],
        held_operations=[a['id'] for a in tx.matching('actions', ('held',))
                         if a.get('redeployment') == hold['id']],
        held_streams=[g['id'] for g in streams], blockers=blockers)


def need_current(tx, ctx, id, generation):
    control(ctx)
    hold = tx.need('_redeployments', id)
    if hold['generation'] != generation:
        raise Fault('Stale redeployment generation', 409)
    if hold['state'] != 'completed' and any(
            tx.get('_redeploy_resources', r) != id for r in hold['resources']):
        raise Fault('Redeployment no longer owns this resource', 409)
    return hold


@op('cap.redeploy.begin', 'id:str resource?:str resources?:list provider?:str service?:str reason?:str expected_version?:str')
def begin(tx, ctx, id, resource='', resources=None, provider='', service='', reason='', expected_version=''):
    """Park pending reservations before firmware, service or resource replacement."""
    control(ctx)
    ident(id)
    supplied = int(bool(resource)) + int(resources is not None) + int(bool(provider)) + int(bool(service))
    if supplied != 1:
        raise Fault('Select exactly one resource, resource list, provider or service')
    if provider:
        selector = {'provider': ident(provider)}
    elif service:
        selector = {'service': ident(service)}
    else:
        values = [resource] if resource else resources
        if not values or any(not isinstance(r, str) for r in values):
            raise Fault('At least one resource identity is required')
        selector = {'resources': sorted(set(ident(r) for r in values))}
    spec = dict(selector=selector, reason=reason, expected_version=expected_version)
    prior = tx.get('_redeployments', id)
    if prior:
        if any(prior.get(k) != v for k, v in spec.items()):
            raise Fault('Redeployment identity conflict', 409)
        if prior['state'] != 'completed':
            reconcile(tx, ctx, id)
        return snapshot(tx, prior)
    offers = tx.rows('offers')
    targets = selector.get('resources') or sorted(set(
        o['resource'] for o in offers if selected({'selector': selector}, o)))
    known = {o['resource'] for o in offers}
    if not targets or any(r not in known for r in targets):
        raise Fault('Redeployment requires registered resource identities', 404)
    if any(current(tx, r) for r in targets):
        raise Fault('Resource is already being redeployed', 409)
    # Check dynamic scopes even when a new resource has not yet registered.
    for other in tx.rows('_redeployments'):
        if other['state'] != 'completed' and other['selector'] == selector:
            raise Fault('Scope is already being redeployed', 409)
    generation = tx.get('_meta', 'redeployment_generation', 0) + 1
    tx.put('_meta', 'redeployment_generation', generation)
    hold = dict(spec, id=id, generation=generation, resources=targets,
                state='redeploying', owner=ctx.actor, started_at=time.time())
    tx.put('_redeployments', id, hold)
    for r in targets:
        tx.put('_redeploy_resources', r, id)
    reconcile(tx, ctx, id)
    tx.emit('redeployment.started', {'id': id, 'resources': targets, 'generation': generation})
    return snapshot(tx, hold)


@op('cap.redeploy.status', 'id?:str resource?:str', False)
def status(tx, ctx, id='', resource=''):
    """Read held reservation identities and actual deployment blockers."""
    control(ctx)
    if bool(id) == bool(resource):
        raise Fault('Identify one redeployment or resource')
    hold = tx.need('_redeployments', id) if id else current(tx, resource)
    return snapshot(tx, hold) if hold else dict(resource=resource, state='active', ready_to_deploy=False)


@op('cap.redeploy.fail', 'id:str generation:int reason:str evidence?:obj')
def failed(tx, ctx, id, generation, reason, evidence=None):
    """Keep reservations held after a failed or uncertain deployment."""
    hold = need_current(tx, ctx, id, generation)
    if hold['state'] == 'completed':
        raise Fault('Redeployment is already completed', 409)
    if not reason.strip():
        raise Fault('Deployment failure reason is required')
    hold.update(state='failed', failure=dict(reason=reason, evidence=evidence or {}, at=time.time()))
    tx.put('_redeployments', id, hold)
    reconcile(tx, ctx, id)
    tx.emit('redeployment.failed', {'id': id, 'generation': generation, 'reason': reason})
    return snapshot(tx, hold)


@op('cap.redeploy.retry', 'id:str generation:int')
def retry(tx, ctx, id, generation):
    """Resume the same held deployment without releasing pending work."""
    hold = need_current(tx, ctx, id, generation)
    if hold['state'] == 'completed':
        raise Fault('Redeployment is already completed', 409)
    hold.update(state='redeploying', retried_at=time.time())
    tx.put('_redeployments', id, hold)
    reconcile(tx, ctx, id)
    return snapshot(tx, hold)


@op('cap.redeploy.quiesce', 'id:str generation:int evidence:obj')
def quiesce(tx, ctx, id, generation, evidence):
    """Record observed control-stream shutdown; never infer it from disconnection."""
    hold = need_current(tx, ctx, id, generation)
    if hold['state'] == 'completed':
        raise Fault('Redeployment is already completed', 409)
    streams = evidence.get('streams', {})
    if not isinstance(streams, dict) or not streams:
        raise Fault('Control-stream quiescence evidence is required')
    for key, proof in streams.items():
        grant = tx.need('_stream_grants', key)
        if grant.get('redeployment') != id or grant['state'] != 'held':
            raise Fault('Stream is not held by this deployment', 409)
        if (not isinstance(proof, dict) or proof.get('quiescent') is not True or
                proof.get('method') not in ('provider_stopped', 'resource_fenced', 'device_offline') or
                not isinstance(proof.get('observation'), dict) or not proof['observation']):
            raise Fault('Observed enforcement-point quiescence is required', 409)
        grant['redeploy_quiescence'] = dict(proof, observed_at=time.time())
        tx.put('_stream_grants', key, grant)
    return snapshot(tx, hold)


def clear_hold_fields(lease):
    for key in ('redeployment', 'held_from', 'held_at', 'held_idle_remaining'):
        lease.pop(key, None)


def cancel_action(tx, ctx, action, reason):
    if action['state'] != 'held':
        return
    action.update(state='failed', result={'cancelled': True, 'executed': False, 'reason': reason})
    action.pop('redeployment', None)
    tx.put('actions', action['id'], action)
    from .authorization import observe
    observe(tx, action['id'], 'failed', action['result'])
    lifecycle.settled(tx, action)
    tx.emit('operation.failed', {'id': action['id']})


def cancel_lease(tx, lease, cancel_actions=False):
    actions = tx.matching('actions', ('held',), lease=lease['id'])
    if actions and not cancel_actions:
        return False
    for action in actions:
        cancel_action(tx, None, action, 'Reservation released during redeployment')
    if lease.get('queue_key'):
        tx.drop('_lease_queue', lease.pop('queue_key'))
    clear_hold_fields(lease)
    lease.update(state='released', released_at=time.time(), expires_at=time.time() + 300)
    tx.put('leases', lease['id'], lease)
    return True


def resume_actions(tx, ctx, lease):
    from .providers import activate_action
    for action in tx.matching('actions', ('held',), lease=lease['id']):
        if current(tx, lease['resource']) and not lease.get('maintenance_deployment'):
            continue
        # Hard execution deadlines, if supplied by a contract, are not paused.
        deadline = action.get('deadline_at')
        if action.get('kill_requested') or (deadline is not None and deadline <= time.time()):
            cancel_action(tx, ctx, action, 'Command cancelled or deadline elapsed during deployment')
            continue
        action.update(state='queued', provider=lease['provider'], resource=lease['resource'],
                      generation=lease['generation'], guard=lease['guard'])
        action.pop('redeployment', None)
        action.pop('held_at', None)
        activate_action(tx, ctx, action)


@op('cap.redeploy.complete', 'id:str generation:int evidence:obj outcome?:str')
def complete(tx, ctx, id, generation, evidence, outcome='deployed'):
    """Resume the same reservations after observed health and version verification."""
    hold = need_current(tx, ctx, id, generation)
    if outcome not in ('deployed', 'rolled_back'):
        raise Fault('Outcome must be deployed or rolled_back')
    if hold['state'] == 'completed':
        if hold.get('outcome') != outcome or hold.get('evidence') != evidence:
            raise Fault('Conflicting deployment completion', 409)
        return snapshot(tx, hold)
    reconcile(tx, ctx, id)
    view = snapshot(tx, hold)
    if view['blockers']:
        raise Fault('Executing work must quiesce before deployment completion', 409)
    observations = evidence.get('resources', {})
    if not isinstance(observations, dict):
        raise Fault('Per-resource verification evidence is required')
    for resource in hold['resources']:
        observation = observations.get(resource, {})
        if (not isinstance(observation, dict) or observation.get('healthy') is not True or
                not isinstance(observation.get('version'), str) or not observation['version']):
            raise Fault('Healthy resource identity and observed version are required', 409)
        if outcome == 'deployed' and hold['expected_version'] and observation['version'] != hold['expected_version']:
            raise Fault('Observed deployment version does not match requested version', 409)
        if not any(o['resource'] == resource and o['state'] == 'ready' for o in tx.rows('offers')):
            raise Fault('Resource has not registered a ready offer', 409)
    from .providers import bind, promote
    held = [l for l in tx.matching('leases', ('held',)) if l.get('redeployment') == id]
    hold.update(state='completed', completed_at=time.time(), outcome=outcome, evidence=evidence)
    tx.put('_redeployments', id, hold)
    for resource in hold['resources']:
        tx.drop('_redeploy_resources', resource)
    # Previously bound reservations precede waiting requests. Existing FIFO
    # keys remain unchanged, including across restart and repeated holds.
    held.sort(key=lambda l: (l.get('held_from') == 'queued', bool(l.get('shared_parent')),
                             l.get('reservation_order', l.get('queue_key', '')), l['id']))
    for lease in held:
        was_queued = lease.get('held_from') == 'queued'
        remaining = lease.get('held_idle_remaining')
        clear_hold_fields(lease)
        lease.pop('shared_parent', None)
        lease.pop('notified', None)
        lease['state'] = 'queued'
        tx.put('leases', lease['id'], lease)
        rebound = None if was_queued else bind(tx, ctx, lease)
        if rebound:
            if remaining is not None:
                idle = lifecycle.policy(tx, rebound)['reservation_policy'].get('idle_seconds', 0)
                if idle:
                    tx.put('_reservation_activity', lease['id'], {
                        'generation': rebound['generation'], 'at': time.time() - max(0, idle - remaining)})
        else:
            if not lease.get('queue_key'):
                lease['queue_key'] = lease.get('reservation_order') or ('00000000000000000000:' + lease['id'])
                tx.put('_lease_queue', lease['queue_key'], lease['id'])
            tx.put('leases', lease['id'], lease)
    promote(tx, ctx)
    for grant in tx.matching('_stream_grants', ('held',)):
        if grant.get('redeployment') != id:
            continue
        lease = tx.get('leases', grant['lease'], {})
        if lease.get('state') not in ('bound', 'fencing'):
            grant.update(state='closed', expires_at=time.time())
        else:
            grant.update(state='open', generation=lease['generation'], provider=lease['provider'],
                         epoch=uid(), expires_at=time.time() + grant.get('held_remaining', 0))
        for key in ('redeployment', 'held_at', 'held_remaining', 'redeploy_quiescence_required', 'redeploy_quiescence'):
            grant.pop(key, None)
        tx.put('_stream_grants', grant['id'], grant)
    tx.emit('redeployment.completed', {'id': id, 'generation': generation, 'outcome': outcome})
    return snapshot(tx, hold)


@op('cap.redeploy.submit', 'id:str generation:int contract:str inputs:obj operation:str resource?:str')
def submit(tx, ctx, id, generation, contract, inputs, operation, resource=''):
    """Run one scoped maintenance command without reopening ordinary admission."""
    hold = need_current(tx, ctx, id, generation)
    if hold['state'] in ('failed', 'completed'):
        raise Fault('Redeployment is not accepting maintenance commands', 409)
    prior = tx.get('_submissions', operation)
    if prior and prior.get('maintenance_deployment') == id:
        from .providers import submit as resubmit
        return resubmit(tx, ctx, contract, resource=resource or prior['resource'], inputs=inputs,
                        operation=operation, maintenance_deployment=id)
    reconcile(tx, ctx, id)
    if not snapshot(tx, hold)['ready_to_deploy']:
        raise Fault('Wait for executing work to quiesce before maintenance', 409)
    matches = [o for o in tx.rows('offers') if o['contract'] == contract and
               o['resource'] in hold['resources'] and (not resource or o['resource'] == resource)]
    targets = sorted(set(o['resource'] for o in matches))
    if len(targets) != 1:
        raise Fault('Select one compatible resource belonging to this deployment', 409)
    from .providers import submit as submit_command
    return submit_command(tx, ctx, contract, resource=targets[0], inputs=inputs,
                          operation=operation, maintenance_deployment=id)


@op('cap.redeploy.prepare', 'id:str generation:int contract:str resource?:str')
def prepare(tx, ctx, id, generation, contract, resource=''):
    """Activate a configurable replacement while ordinary reservations stay held."""
    hold = need_current(tx, ctx, id, generation)
    if hold['state'] in ('failed', 'completed'):
        raise Fault('Redeployment is not accepting activation', 409)
    reconcile(tx, ctx, id)
    if not snapshot(tx, hold)['ready_to_deploy']:
        raise Fault('Wait for executing work to quiesce before activation', 409)
    targets = sorted(set(o['resource'] for o in tx.rows('offers')
        if o['contract'] == contract and o['resource'] in hold['resources']
        and (not resource or o['resource'] == resource)))
    if len(targets) != 1:
        raise Fault('Select one configurable resource belonging to this deployment', 409)
    from .extensions import prepare as activate
    return activate(tx, ctx, contract, targets[0], maintenance_deployment=id)
