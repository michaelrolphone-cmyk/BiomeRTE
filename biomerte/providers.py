"""Capability contracts, mutable offers, leases, fences and durable dispatch."""
import math
import time
from .api import OPERATIONS, op, ident, validate, schema
from .capability_usage import caller_usage, published_usage
from .store import Fault, uid, text, digest
from . import lifecycle, placement as selection, redeployment


def owner(ctx, provider):
    if ctx.permissions is not None and ctx.actor != provider:
        raise Fault('Provider identity mismatch', 403)


def contract_record(name, inputs, outputs, version=1, description='', synchronous=False, usage=None):
    key = ident(name) + ':' + str(version)
    if version < 1:
        raise Fault('Invalid contract version')
    return {'id': key, 'name': name, 'version': version, 'inputs': schema(inputs),
            'outputs': schema(outputs), 'description': description, 'synchronous': synchronous}


@op('cap.define', 'name:str inputs:obj outputs:obj version?:int description?:str synchronous?:bool usage?:obj')
def define(tx, ctx, name, inputs, outputs, version=1, description='', synchronous=False, usage=None):
    """Define an immutable executable schema; optionally publish revisable caller instructions and example_inputs."""
    record = contract_record(name, inputs, outputs, version, description, synchronous)
    key = record['id']
    prior = tx.get('contracts', key) or tx.get('_retired_contracts', key)
    if prior:
        from .retention import require_payload
        require_payload(prior)
    if prior and prior != record:
        raise Fault('Contract version is immutable', 409)
    if usage is not None:
        tx.put('_contract_usage', key, published_usage(record, usage))
    return tx.put('contracts', key, record)


OFFER_FIELDS = 'provider:str contract:str resource:str state?:str service?:str configuration?:obj capacity?:int usage?:obj lease_class?:str'


@op('cap.offer', OFFER_FIELDS + ' expected_registration?:str expected_absent?:bool publication_id?:str')
def offer(tx, ctx, provider, contract, resource, state='ready', service='', configuration=None, capacity=None,
          usage=None, lease_class='', expected_registration=None, expected_absent=False, publication_id=None):
    """Publish or revise an offer. Shared physical resources use the same resource ID."""
    owner(ctx, provider)
    if ctx.permissions is not None and provider in ('builtin', 'routine'):
        raise Fault('Reserved provider', 403)
    if ctx.permissions is not None and ('offer:' + contract not in ctx.permissions or 'resource:' + resource not in ctx.permissions):
        raise Fault('Offer or resource outside provider grant', 403)
    definition = tx.need('contracts', contract)
    key = digest([provider, contract, resource])
    previous = tx.get('offers', key, {})
    if publication_id is not None:
        ident(publication_id)
    if expected_absent and expected_registration is not None:
        raise Fault('Choose either an absent offer or an existing registration')
    if expected_absent and previous:
        raise Fault('Offer already exists', 409)
    if expected_registration is not None:
        if (not previous or previous.get('service') != service
                or previous.get('registration') != (expected_registration or None)):
            raise Fault('Superseded offer registration', 409)
    configuration = dict(previous.get('configuration', {}) if configuration is None else configuration)
    if lease_class:
        configuration['lease_class'] = lease_class
    configuration = lifecycle.configuration(configuration, provider in ('builtin', 'routine'))
    policy = configuration.get('reservation_policy', {})
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
    rec = {'id': key, 'provider': ident(provider), 'contract': contract, 'resource': ident(resource),
           'state': state, 'service': service, 'configuration': configuration or {}, 'registration': uid()}
    if publication_id is not None:
        tx.put('_offer_provenance', key, dict(registration=rec['registration'], published_by=publication_id))
    elif tx.get('_offer_provenance', key):
        tx.drop('_offer_provenance', key)
    if capacity is not None:
        if capacity < 0:
            raise Fault('Capacity must be 1 (exclusive), a positive slot count, or 0 (unbounded)')
        tx.put('_capacity', resource, capacity)
    if usage is not None:
        tx.put('_offer_usage', key, published_usage(definition, usage))
    redeployment.register_offer(tx, ctx, rec)
    tx.put('offers', key, rec)
    tx.emit('offer.updated', {'offer': key, 'state': state})
    if state == 'ready':
        observe_contact(ctx, provider, resource)
    return dict(rec, capacity=capacity_of(tx, rec))



@op('cap.withdraw', 'provider:str resource:str service:str registrations:obj')
def withdraw(tx, ctx, provider, resource, service, registrations):
    """Withdraw this provider's exact offer revisions together. A successor stays ready."""
    owner(ctx, provider)
    if not 1 <= len(registrations) <= 128:
        raise Fault('Withdraw between one and 128 offer registrations')
    if ctx.permissions is not None and (provider in ('builtin', 'routine') or
            'resource:' + resource not in ctx.permissions):
        raise Fault('Resource outside provider grant', 403)
    for contract, revision in registrations.items():
        if not isinstance(contract, str):
            raise Fault('Invalid withdrawal contract')
        ident(contract)
        if revision is not None and (not isinstance(revision, str) or len(revision) != 32
                or any(c not in '0123456789abcdef' for c in revision)):
            raise Fault('Invalid offer registration')
        if ctx.permissions is not None and 'offer:' + contract not in ctx.permissions:
            raise Fault('Offer outside provider grant', 403)
    withdrawn, superseded = [], []
    for contract, revision in sorted(registrations.items()):
        key = digest([provider, contract, resource])
        row = tx.get('offers', key)
        if not row or row.get('service') != service or row.get('registration') != revision:
            superseded.append(contract)
            continue
        was_online = row['state'] != 'offline'
        # Fence a restore already in transit even when the offer is currently offline.
        row.update(state='offline', registration=uid())
        tx.put('_offer_provenance', key, dict(registration=row['registration'], withdrawn_from=revision))
        tx.put('offers', key, row)
        if was_online:
            tx.emit('offer.updated', {'offer': key, 'state': 'offline'})
        withdrawn.append(contract)
    return {'withdrawn': withdrawn, 'superseded': superseded}


def authorize_offer_page(ctx, provider, resource, contracts):
    owner(ctx, provider)
    if not 1 <= len(contracts) <= 16 or any(not isinstance(key, str) for key in contracts):
        raise Fault('Provide between one and sixteen contract identifiers')
    if len({ident(key) for key in contracts}) != len(contracts):
        raise Fault('Duplicate contract in offer page')
    if ctx.permissions is not None and (not ctx.allowed('cap.offer') or provider in ('builtin', 'routine') or
            'resource:' + resource not in ctx.permissions or
            any('offer:' + key not in ctx.permissions for key in contracts)):
        raise Fault('Offer or resource outside provider grant', 403)


def registration_result(contract, status, row=None):
    result = dict(contract=contract, status=status, state=None, registration=None)
    if row is not None:
        result.update(state=row['state'], registration=row.get('registration'))
    return result


@op('cap.registrations', 'provider:str resource:str service:str contracts:list', write=False)
def registrations(tx, ctx, provider, resource, service, contracts):
    """Read exact enrolled offers, including revision provenance for restart reconciliation."""
    authorize_offer_page(ctx, provider, resource, contracts)
    result = []
    for contract in contracts:
        key = digest([provider, contract, resource])
        row = tx.get('offers', key)
        if row and row.get('service') == service:
            receipt = registration_result(contract, 'found', row)
            proof = tx.get('_offer_provenance', key, {})
            if proof.get('registration') == row.get('registration'):
                receipt.update({k:proof[k] for k in ('withdrawn_from', 'published_by') if k in proof})
            result.append(receipt)
        else:
            result.append(registration_result(contract, 'superseded' if row else 'missing'))
    return {'registrations': result}


def restore_offer(tx, ctx, provider, resource, service, entry, publication_id):
    desired, definition = entry['offer'], entry['definition']
    contract = desired['contract']
    key = digest([provider, contract, resource])
    row = tx.get('offers', key)
    if row is None:
        return registration_result(contract, 'missing')
    if (row.get('service') != service or row.get('registration') != entry['expected_registration']
            or tx.get('_enforcers', resource) not in (None, provider)):
        return registration_result(contract, 'superseded')
    try:
        if set(desired) - {'contract', 'state', 'configuration', 'capacity', 'usage', 'lease_class'}:
            raise Fault('Invalid nested offer fields')
        validate(OPERATIONS['cap.offer'][1], dict(desired, provider=provider, resource=resource, service=service))
        validate(OPERATIONS['cap.define'][1], definition)
        record = contract_record(**definition)
        old = tx.get('contracts', contract)
        prior = old or tx.get('_retired_contracts', contract)
        if prior:
            from .retention import require_payload
            require_payload(prior)
        state = desired.get('state', 'ready')
        if record['id'] != contract or (prior and prior != record) or state not in ('ready', 'available_after_setup'):
            raise Fault('Conflicting contract or nonpublishable state')
        if 'usage' in definition:
            published_usage(record, definition['usage'])
        if 'usage' in desired:
            published_usage(record, desired['usage'])
        configuration = dict(desired.get('configuration', row.get('configuration', {})))
        if desired.get('lease_class'):
            configuration['lease_class'] = desired['lease_class']
        config = lifecycle.configuration(configuration, provider in ('builtin', 'routine'))
    except (Fault, ValueError, TypeError):
        return registration_result(contract, 'invalid')
    if (config != lifecycle.configuration(row.get('configuration'), provider in ('builtin', 'routine'))
            or ('capacity' in desired and desired['capacity'] != capacity_of(tx, row))
            or ('usage' in desired and desired['usage'] != tx.get('_offer_usage', key))):
        return registration_result(contract, 'different', row)
    guide_changed = 'usage' in definition and definition['usage'] != tx.get('_contract_usage', contract)
    if old is None or guide_changed:
        if not ctx.allowed('cap.define'):
            raise Fault('Definition change requires cap.define', 403)
        define(tx, ctx, **definition)
    elif row['state'] == state:
        return registration_result(contract, 'unchanged', row)
    row.update(state=state, registration=uid())
    redeployment.register_offer(tx, ctx, row)
    if publication_id is not None:
        tx.put('_offer_provenance', key, dict(registration=row['registration'], published_by=publication_id))
    elif tx.get('_offer_provenance', key):
        tx.drop('_offer_provenance', key)
    tx.put('offers', key, row)
    tx.emit('offer.updated', {'offer':key, 'state':state})
    return registration_result(contract, 'published', row)


@op('cap.publish', 'provider:str resource:str service:str offers:list publication_id?:str')
def publish(tx, ctx, provider, resource, service, offers, publication_id=None):
    """Restore existing scoped offers from exact prior revisions, retaining active leases and settings."""
    if publication_id is not None:
        ident(publication_id)
    if not 1 <= len(offers) <= 16:
        raise Fault('Provide between one and sixteen offers')
    contracts = []
    for entry in offers:
        if (not isinstance(entry, dict) or set(entry) != {'definition', 'offer', 'expected_registration'}
                or not isinstance(entry['offer'], dict) or not isinstance(entry['definition'], dict)):
            raise Fault('Exact definition, offer and expected registration required')
        rev = entry['expected_registration']
        if rev is not None and (not isinstance(rev, str) or len(rev) != 32 or any(c not in '0123456789abcdef' for c in rev)):
            raise Fault('Invalid expected offer registration')
        contracts.append(entry['offer'].get('contract'))
    authorize_offer_page(ctx, provider, resource, contracts)
    results = [restore_offer(tx, ctx, provider, resource, service, entry, publication_id) for entry in offers]
    if any(result['status'] in ('published', 'unchanged') and result['state'] == 'ready' for result in results):
        observe_contact(ctx, provider, resource)
    return {'offers': results}


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
    if contract and any(l['contract'] == cid for l in tx.matching('leases', ('held',))):
        raise Fault('Contract has held deployment reservations', 409)
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
    if offer and tx.get('_offer_provenance', key):
        tx.drop('_offer_provenance', key)
    if tx.get(usage_kind, key) is not None:
        tx.drop(usage_kind, key)
    tx.drop(kind, key)
    event = tx.emit('capability.deleted', {'kind': kind, 'id': key, 'contract': cid, 'actor': ctx.actor})
    return {'deleted': key, 'kind': kind, 'audit_event': event['id']}


def observe_contact(ctx, provider, resource=None):
    if getattr(ctx, 'authenticated_provider', None) == provider:
        ctx.app.provider_contacts[(provider, resource)] = time.monotonic()


def effective_offer(tx, ctx, offer):
    """A restored native execution offer needs a worker in contact with this authority."""
    app = ctx.app
    cfg = offer.get('configuration', {})
    if (not getattr(app, 'controller_runtime', None) or offer['state'] != 'ready'
            or not cfg.get('execution_custody')):
        return offer
    provider = offer['provider']
    peer = tx.get('peers', provider)
    if not peer or 'provider.poll' not in peer.get('scopes', []):
        return offer  # Delegated publishers use the existing offer registration lifecycle.
    seen = max(app.provider_contacts.get((provider, None), float('-inf')),
               app.provider_contacts.get((provider, offer['resource']), float('-inf')))
    lifetime = cfg.get('heartbeat_interval_seconds', 60) * cfg.get('missed_heartbeats_limit', 3)
    if peer['enabled'] and time.monotonic() - seen < lifetime:
        return offer
    return dict(offer, state='offline', advertised_state=offer['state'],
                availability_reason='no_recent_signed_provider_contact')


@op('cap.discover', 'contract?:str', False)
def discover(tx, ctx, contract=''):
    """List capabilities with per-contract and provider-specific call examples, prerequisites and result paths."""
    definitions = {d['id']: d for d in tx.rows('contracts') if not contract or d['id'] == contract}
    guides = {key: tx.get('_contract_usage', key, {}) for key in definitions}
    contracts = [dict(definitions[key], usage=caller_usage(definitions[key], guides[key]),
                      placement=selection.GUIDANCE, implementations=[]) for key in sorted(definitions)]
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
        off = effective_offer(tx, ctx, off)
        offers.append(dict(off, capacity=capacity_of(tx, off),
                           usage=caller_usage(definitions[key], guide, off['resource'], off.get('configuration', {}).get('stream'))))
    by_contract = {item['id']: item for item in contracts}
    for item in offers:
        by_contract[item['contract']]['implementations'].append(dict(
            selection.summary(item), offer=item['id'], state=item['state'],
            admitted=admitted(tx, item), capacity=item['capacity']))
    return {'contracts': contracts, 'offers': offers}


def admitted(tx, off, maintenance_deployment=''):
    if not redeployment.allowed(tx, off, maintenance_deployment):
        return False
    policy = tx.get('mission', 'current', {}).get('allocation', {})
    mode = policy.get(off['service'])
    accepted_modes = off.get('configuration', {}).get('modes', [])
    return off['state'] == 'ready' and (off['service'] not in policy or
                                        (mode is not None and (not accepted_modes or mode in accepted_modes)))


def capacity_of(tx, offer):
    return tx.get('_capacity', offer['resource'], 0 if offer['provider'] in ('builtin', 'routine') else 1)


def active_on(tx, resource):
    return [l for l in tx.matching('leases', ('fencing', 'bound')) if l['resource'] == resource and not l.get('shared_parent')]


def reservation_policy(tx, lease):
    configuration = lifecycle.policy(tx, lease)
    policy = configuration['reservation_policy']
    if ('idle_seconds' not in policy and lease['state'] == 'bound'
            and configuration['lease_class'] != 'persistent' and policy['release_after_result']):
        # A disconnected caller can disappear between fence acknowledgement and
        # dispatch. Use the command's own watch interval for that empty slot.
        from .shared_streams import enabled
        if not enabled(tx.get('offers', lease.get('offer', ''), {})):
            return dict(policy, idle_seconds=configuration['watch_ttl_seconds'])
    return policy


def touch_reservation(tx, lease):
    # Only pre-dispatch reservations have an idle clock. This is never
    # an operation deadline: queued/claimed/uncertain work retains its fence.
    if lease['state'] in ('fencing', 'bound') and reservation_policy(tx, lease).get('idle_seconds', 0) > 0:
        tx.put('_reservation_activity', lease['id'], {'generation': lease['generation'], 'at': time.time()})


def recover_reservations(tx, ctx):
    redeployment.reconcile(tx, ctx)
    now = time.time()
    recovered = 0
    for lease in tx.matching('leases', ('bound', 'fencing')):
        if redeployment.target(tx, lease):
            continue  # Executing work keeps custody; held intent has no expiry clock.
        lease_actions = tx.matching('actions', lease=lease['id'])
        if lifecycle.recover(tx, ctx, lease, lease_actions, now):
            recovered += 1
            continue
        policy = reservation_policy(tx, lease)
        if not policy:
            continue
        work = [a for a in lease_actions if a['generation'] == lease['generation']]
        if any(a['state'] not in lifecycle.TERMINAL for a in work):
            continue
        # Combined submissions retain enough information to repair dispatch.
        if tx.get('_submission_leases', lease['id']) and not work:
            start_submission(tx, ctx, lease)
            continue
        reason = None
        if work and (any(a.get('release_after_result', policy.get('release_after_result', False)) for a in work)):
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
    lifecycle.sweep(tx, ctx, now)
    return recovered


def notify_bound(tx, ctx, lease):
    if lease.get('reply_to') and not lease.get('notified'):
        from .api import send_operational, Context
        body = {k: lease[k] for k in ('id', 'task', 'contract', 'resource', 'generation', 'reply_to')}
        body.update(type='lease.available', state='bound')
        send_operational(tx, Context(ctx.app, actor='controller', scope=ctx.scope), lease['reply_to'], body, lease['id'])
        tx.emit('lease.available', body)
        lease['notified'] = True


def slot_guard(resource, slot):
    return resource if slot == 0 else '!slot:%s:%d' % (resource, slot)


def bind(tx, ctx, request):
    if request.get('state') == 'held':
        return None
    for of in tx.rows('offers'):
        resource = of['resource']
        if of['contract'] != request['contract']:
            continue
        of = effective_offer(tx, ctx, of)
        if not admitted(tx, of, request.get('maintenance_deployment', '')):
            continue
        if not selection.matches(of, request['requested_resource'], request.get('placement')):
            continue
        if any(a['resource'] == resource and a['state'] == 'pending' for a in tx.rows('activations')):
            continue
        from .shared_streams import join
        shared = join(tx, ctx, request, of)
        if shared:
            notify_bound(tx, ctx, shared)
            redeployment.resume_actions(tx, ctx, shared)
            return shared
        capacity = capacity_of(tx, of)
        if capacity:
            active = active_on(tx, resource)
            if len(active) >= capacity:
                continue
            # Slot zero uses the original guard and on-device fence key.
            held = {l['guard'] for l in active}
            for slot in range(len(active) + 1):
                guard_key = slot_guard(resource, slot)
                if guard_key not in held:
                    break
        else:
            guard_key = '!shared:' + digest([resource, request['id']])
        guard = tx.get('guards', guard_key, {'generation': tx.get('_fence_generations', resource, 0), 'holder': ''})
        if guard['holder']:
            continue
        generation = max(guard['generation'], request.get('generation', 0)) + 1
        lease_policy = lifecycle.policy(tx, {'offer': of['id'], 'provider': of['provider']})
        lease = dict(request, offer=of['id'], provider=of['provider'], resource=resource,
                     generation=generation, guard=guard_key,
                     state='bound' if of['provider'] in ('builtin', 'routine') else 'fencing',
                     lease_class=lease_policy['lease_class'], lease_policy=lease_policy,
                     fencing_since=time.time(), last_progress_at=time.time(), missed_heartbeats=0)
        tx.put('guards', guard_key, {'generation': generation, 'holder': lease['id'], 'resource': resource})
        if lease.get('queue_key'):
            lease.setdefault('reservation_order', lease['queue_key'])
            tx.drop('_lease_queue', lease.pop('queue_key'))
        event = tx.emit('lease.acquired', {'lease': lease['id'], 'generation': generation})
        lease.setdefault('reservation_order', '%020d' % event['seq'])
        if lease['state'] == 'bound':
            notify_bound(tx, ctx, lease)
        tx.put('leases', lease['id'], lease)
        touch_reservation(tx, lease)
        redeployment.resume_actions(tx, ctx, lease)
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


@op('cap.acquire', 'task:str contract:str resource?:str enqueue?:bool reply_to?:str placement?:obj')
def acquire(tx, ctx, task, contract, resource='', enqueue=True, reply_to='', placement=None, maintenance_deployment=''):
    """Acquire or queue. Use distinct task IDs for independent sessions. The worker fences automatically."""
    tx.need('contracts', contract)
    text(task, 'task')
    if reply_to:
        text(reply_to, 'reply_to')
    resource, placement = selection.normalize(resource, placement)
    key = selection.identity(task, contract, resource, placement)
    batch_full = promote(tx, ctx)
    old = tx.get('leases', key, {})
    if old.get('state') in ('queued', 'bound', 'fencing', 'held') and ctx.permissions is not None and old.get('owner') != ctx.actor:
        raise Fault('Lease belongs to another task owner', 403)
    if old.get('state') in ('queued', 'held'):
        return redeployment.hold_request(tx, ctx, old)
    if old.get('state') in ('bound', 'fencing'):
        if redeployment.target(tx, old):
            return redeployment.hold_request(tx, ctx, old)
        if admitted(tx, tx.need('offers', old['offer']), old.get('maintenance_deployment', '')):
            if old.get('shared_owner_released') and old.get('owner') == ctx.actor:
                old.pop('shared_owner_released')
                tx.put('leases', old['id'], old)
            touch_reservation(tx, old)
            return old
        if not release(tx, old):
            return old
        batch_full = promote(tx, ctx)
    req = {'id': key, 'task': task, 'contract': contract, 'resource': resource,
           'requested_resource': resource, 'generation': old.get('generation', 0), 'owner': ctx.actor,
           'provider': '', 'reply_to': reply_to}
    if placement:
        req['placement'] = placement
    if maintenance_deployment:
        req['maintenance_deployment'] = maintenance_deployment
    lease = None if batch_full else bind(tx, ctx, req)
    if lease:
        return lease
    if not enqueue:
        raise Fault('No compatible available resource', 409)
    event = tx.emit('lease.queued', {'lease': key, 'task': task, 'contract': contract})
    req.update(state='queued', queue_key='%020d' % event['seq'],
               reply_to=reply_to or (ctx.actor if ctx.permissions is not None else task))
    req['reservation_order'] = req['queue_key']
    tx.put('_lease_queue', req['queue_key'], key)
    tx.put('leases', key, req)
    return redeployment.hold_request(tx, ctx, req)


@op('cap.lease', 'lease:str', receipt='refresh')
def lease_status(tx, ctx, lease):
    """Read current lease and refresh its reservation heartbeat; new request IDs retain no receipt."""
    value = tx.need('leases', lease)
    if ctx.permissions is not None and value.get('owner') != ctx.actor:
        raise Fault('Lease belongs to another task owner', 403)
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
    guard = tx.get('guards', l['guard'], {'holder':'','generation':0})
    if l['state'] not in ('bound', 'fencing') or (l['provider'], guard['holder'], guard['generation']) != (provider, lease, generation):
        raise Fault('Superseded fence', 409)
    if l['state'] == 'fencing':
        l['state'] = 'bound'
        touch_reservation(tx, l)
    notify_bound(tx, ctx, l)
    return tx.put('leases', lease, l)


def release(tx, l, cancel_held=False):
    if l['state'] == 'released':
        return True
    if l['state'] == 'held':
        return redeployment.cancel_lease(tx, l, cancel_held)
    held_actions = tx.matching('actions', ('held',), lease=l['id'])
    if held_actions and not cancel_held:
        return False
    for action in held_actions:
        redeployment.cancel_action(tx, None, action, 'Reservation released during redeployment')
    from .shared_streams import release as release_subscription
    if release_subscription(tx, l):
        return True
    if l['state'] == 'queued':
        tx.drop('_lease_queue', l.pop('queue_key'))
        l.update(state='released', released_at=time.time(), expires_at=time.time() + 300)
        tx.put('leases', l['id'], l)
        return True
    if any(a['state'] in ('queued', 'claimed', 'uncertain')
           for a in tx.matching('actions', 'lease', (l['id'],))):
        return False
    guard = tx.need('guards', l['guard'])
    if guard['holder'] == l['id'] and guard['generation'] == l['generation']:
        guard.update(holder='', released_at=time.time())
        tx.put('guards', l['guard'], guard)
        tx.put('_fence_generations', l['resource'], max(guard['generation'], tx.get('_fence_generations', l['resource'], 0)))
    l.update(state='released', released_at=time.time(), expires_at=time.time() + 300)
    tx.put('leases', l['id'], l)
    return True


def release_task(tx, task):
    for l in tx.matching('leases', 'task', (task,)):
        if l['state'] != 'released':
            release(tx, l, cancel_held=True)


@op('cap.release', 'lease:str')
def release_lease(tx, ctx, lease):
    """Release a held lease after work settles, or cancel a queued request. Results remain available via cap.status."""
    l = tx.need('leases', lease)
    if ctx.permissions is not None and l.get('owner') != ctx.actor:
        raise Fault('Lease release is controlled by the task owner', 403)
    if not release(tx, l, cancel_held=True):
        raise Fault('Reconcile outstanding operation before release', 409)
    promote(tx, ctx)
    return l


@op('cap.invoke', 'lease:str inputs:obj operation:str destination?:str release_after_result?:bool')
def dispatch(tx, ctx, lease, inputs, operation, destination='', release_after_result=None):
    """Submit once with a unique operation ID. Read cap.status with that ID for the result; the lease remains held until released."""
    l = tx.need('leases', lease)
    if ctx.permissions is not None and l.get('owner') != ctx.actor:
        raise Fault('Lease belongs to another task owner', 403)
    key = ident(operation)
    prev = tx.get('actions', key)
    if prev:
        from .retention import require_payload
        require_payload(prev)
        if (prev['lease'] != lease or prev['inputs'] != inputs or prev.get('destination', '') != destination or
                release_after_result is not None and prev.get('release_after_result') != release_after_result):
            raise Fault('Operation identity conflict', 409)
        return prev
    from .completion_receipts import find
    if find(tx, key):
        raise Fault('Operation result expired; inspect the retained completion receipt, do not replay', 410)
    if destination:
        lifecycle.destination(tx, ctx, destination)
    hold = redeployment.target(tx, l)
    if tx.get('offers', l.get('offer', ''), {}).get('configuration', {}).get('stream_only'):
        raise Fault('Use stream.open for this streaming interface', 409)
    contract = tx.need('contracts', l['contract'])
    validate(contract['inputs'], inputs)
    if not hold:
        if l['state'] not in ('bound', 'fencing'):
            raise Fault('Lease unavailable', 409)
        offer = effective_offer(tx, ctx, tx.need('offers', l['offer']))
        if not admitted(tx, offer, l.get('maintenance_deployment', '')):
            raise Fault('Lease unavailable', 409)
    from .authorization import guard, observe
    guard(tx, key, l['contract'], l['resource'], inputs, ctx)
    a = {'id': key, 'contract': l['contract'], 'lease': lease, 'provider': l.get('provider', ''),
         'resource': l['resource'], 'generation': l['generation'], 'state': 'held' if hold else 'queued', 'inputs': inputs, 'guard': l.get('guard', ''),
         'destination': destination, 'release_after_result': (reservation_policy(tx, l).get('release_after_result', False) if release_after_result is None else release_after_result), 'created_at': time.time(), 'expires_at': None,
         'status_query': {'operation': 'cap.status', 'arguments': {'operation': key}}}
    if hold:
        a.update(redeployment=hold['id'], held_at=time.time())
    return activate_action(tx, ctx, a)


def activate_action(tx, ctx, a):
    from .authorization import observe
    key, inputs = a['id'], a['inputs']
    l = tx.need('leases', a['lease'])
    if a['state'] == 'held':
        tx.put('actions', key, a)
        tx.emit('operation.held', {'id': key, 'redeployment': a['redeployment']})
        return a
    if l['provider'] == 'builtin' and l['contract'] == 'core.echo:1':
        a.update(state='succeeded', result={'value': inputs['value']})
    if l['provider'] == 'builtin' and l['contract'] == 'human.decision:1':
        from .decisions import start_action
        start_action(tx, ctx, a)
    if l['provider'] == 'builtin' and l['contract'] == 'assistant.controller.command:1':
        from .assistant_support import start_controller_action
        start_controller_action(tx, ctx, a)
    if l['provider'] == 'routine':
        from .process import new_run
        spec = tx.need('offers', l['offer'])['configuration']
        child = new_run(tx, spec['process'], inputs, parent='action:' + key, pin=spec['revision'])
        a.update(state='claimed', child=child['id'])
    tx.put('actions', key, a)
    lifecycle.settled(tx, a)
    observe(tx, key, a['state'], a.get('result'))
    tx.emit('operation.' + a['state'], {'id': key})
    return a


@op('provider.poll', 'provider:str limit?:int lease?:str generation?:int', False)
def pending(tx, ctx, provider, limit=64, lease='', generation=None):
    """Provider work inbox. Completed actions leave this inbox and remain available via cap.status."""
    owner(ctx, provider)
    if limit < 1:
        raise Fault('Provider inbox page size must be positive')
    if lease or generation is not None:
        if not lease or type(generation) is not int or generation < 1:
            raise Fault('Exact lease and generation required')
        current = tx.get('leases', lease, {})
        empty = {'fences': [], 'actions': [], 'activations': [], 'streams': []}
        if (current.get('provider') != provider or current.get('generation') != generation or
                current.get('state') not in ('fencing', 'bound')):
            return empty
        held = redeployment.target(tx, current)
        if current['state'] == 'fencing':
            if not held:
                empty['fences'] = [current]
            observe_contact(ctx, provider, current['resource'])
            return empty
        empty['actions'] = [a for a in tx.matching('actions', ('queued', 'claimed'), lease=lease)
                            if a['provider'] == provider and a['generation'] == generation and
                            (a['state'] == 'claimed' or not held)][:limit]
        from .streams import valid as valid_stream
        for grant in tx.matching('_stream_grants', ('open',), lease=lease):
            if grant['provider'] == provider and grant['generation'] == generation:
                try:
                    empty['streams'].append(valid_stream(tx, ctx, grant))
                except Fault:
                    continue
        observe_contact(ctx, provider, current['resource'])
        return empty
    leases = [l for l in tx.matching('leases', ('fencing',)) if l['provider'] == provider
              and not redeployment.target(tx, l)]
    actions = [a for a in tx.matching('actions', ('queued', 'claimed')) if a['provider'] == provider and tx.need('leases', a['lease'])['state'] == 'bound'
               and (a['state'] == 'claimed' or not redeployment.target(tx, tx.need('leases', a['lease'])))]
    activations = [a for a in tx.matching('activations', ('pending',)) if a['provider'] == provider]
    first, rest, seen = [], [], set()
    for a in actions:
        guard = a.get('guard', a['resource'])
        (rest if guard in seen else first).append(a)
        seen.add(guard)
    from .streams import valid as valid_stream
    streams = []
    for grant in tx.matching('_stream_grants', ('open',)):
        if grant['provider'] != provider:
            continue
        try:
            # Reconnection must use the same authority checks as stream.attach.
            streams.append(valid_stream(tx, ctx, grant))
        except Fault:
            continue
    observe_contact(ctx, provider)
    return {'fences': leases, 'actions': (first + rest)[:min(limit, 64)], 'activations': activations, 'streams': streams}


@op('provider.claim', 'provider:str operation:str generation:int')
def claim(tx, ctx, provider, operation, generation):
    executor(tx, ctx, provider)
    a = tx.need('actions', operation)
    from .retention import require_payload
    require_payload(a)
    if a['state'] in lifecycle.TERMINAL or a['state'] == 'held':
        raise Fault('Superseded or held generation', 409)
    l = tx.need('leases', a['lease'])
    a['lease_policy'] = lifecycle.policy(tx, l)
    g = tx.get('guards', l['guard'], {'holder':'','generation':0})
    if (a['provider'], l['state'], a['generation'], g['holder'], g['generation']) != (
            provider, 'bound', generation, a['lease'], generation):
        raise Fault('Stale resource generation', 409)
    if a['state'] == 'queued':
        from .authorization import guard, observe
        try:
            guard(tx, operation, a['contract'], a['resource'], a['inputs'], ctx)
        except Fault as error:
            a.update(state='failed', result={'authorization_blocked': True, 'reason': str(error)})
            tx.put('actions', operation, a)
            observe(tx, operation, 'failed', a['result'])
            lifecycle.settled(tx, a)
            finish_submission(tx, ctx, operation)
            if a.get('release_after_result', reservation_policy(tx, l).get('release_after_result')):
                release(tx, l)
                promote(tx, ctx)
            return a
        if not admitted(tx, tx.need('offers', l['offer']), l.get('maintenance_deployment', '')):
            raise Fault('Dispatch paused by availability, mission or redeployment', 409)
        now = time.time()
        cfg = lifecycle.policy(tx, l)
        a.update(state='claimed', claimed_at=now, last_progress_at=now, watch_until=now + cfg['watch_ttl_seconds'])
        from .resource_recovery import protected
        l.update(watch_until=a['watch_until'], last_progress_at=now, missed_heartbeats=0,
                 execution_custody=protected(tx,l))
        tx.put('leases', l['id'], l)
        tx.put('actions', operation, a)
        observe(tx, operation, 'claimed')
    return dict(a, output_schema=tx.need('contracts', a['contract'])['outputs'])


@op('provider.result', 'provider:str operation:str generation:int state:str result:obj')
def result(tx, ctx, provider, operation, generation, state, result):
    executor(tx, ctx, provider)
    a = tx.get('actions', operation)
    if not a:
        from .completion_receipts import acknowledge
        return acknowledge(tx, provider, operation, generation, state, result)
    if a['provider'] != provider or a['generation'] != generation:
        raise Fault('Result binding mismatch', 409)
    if state not in ('succeeded', 'failed', 'uncertain'):
        raise Fault('Unknown outcome')
    if a['state'] == 'abandoned':
        # A signed completion is still evidence after its lease was superseded.
        # Preserve abandonment and never rebind a guard, release a newer lease,
        # or emit the success event used by ordinary workflow continuations.
        if a['contract'] not in ('host.terminal.run:1', 'docker.cli:1') or state not in ('succeeded', 'failed'):
            raise Fault('Superseded generation', 409)
        if a.get('execution_state'):
            if a['execution_state'] == state and (
                    a.get('result') == result or
                    (a.get('result_expired') and a.get('result_digest') == digest(result))):
                return a
            raise Fault('Conflicting late execution outcome', 409)
        if state == 'succeeded':
            validate(tx.need('contracts', a['contract'])['outputs'], result)
        a.update(execution_state=state, reconciled_at=time.time(),
                 abandonment_result=a.get('result', {}))
        if a.get('result_expired'):
            # Preserve the established output-expiry boundary: retain the new
            # receipt digest without restoring discarded bulk result payloads.
            a['result_digest'] = digest(result)
        else:
            a['result'] = result
        tx.put('actions', operation, a)
        from .completion_receipts import remember
        remember(tx, a)
        tx.emit('operation.reconciled', {'id': operation, 'execution_state': state,
                                       'state': 'abandoned'})
        return a
    if a.get('result_expired'):
        if a['state'] == state and a.get('result_digest') == digest(result):
            return a  # exact lost-ACK redelivery, no effects or payload restoration
        raise Fault('Conflicting expired outcome', 409)
    if a['state'] in ('succeeded', 'failed'):
        if a['state'] == state and a['result'] == result:
            return a
        raise Fault('Conflicting outcome', 409)
    lifecycle.current(tx, a)
    if a['state'] not in ('claimed', 'uncertain'):
        raise Fault('Operation has not been claimed', 409)
    if state in ('succeeded', 'failed'):
        from .resource_recovery import protected
        held = tx.need('leases', a['lease'])
        proof = a.get('execution_reconciliation', {})
        if protected(tx, held) and not (
                a.get('custody', {}).get('state') in ('finished', 'not_started') or
                result.get('evidence', {}).get('quiescent') is True or
                (proof.get('quiescent') is True and proof.get('result_digest') == digest(result))):
            raise Fault('Executor settlement proof required before resource release', 409)
    if state == 'succeeded':
        validate(tx.need('contracts', a['contract'])['outputs'], result)
    a.update(state=state, result=result)
    a.pop('recovery', None)
    lease = tx.get('leases', a['lease'])
    if lease and lease.pop('recovery_required', None):
        tx.put('leases', lease['id'], lease)
    tx.put('actions', operation, a)
    from .authorization import observe
    observe(tx, operation, state, result)
    tx.emit('operation.' + state, {'id': operation})
    lifecycle.settled(tx, a)
    finish_submission(tx, ctx, operation)
    if state in ('succeeded', 'failed') and a.get('release_after_result', reservation_policy(tx, tx.need('leases', a['lease'])).get('release_after_result')):
        if release(tx, tx.need('leases', a['lease'])):
            promote(tx, ctx)
    redeployment.reconcile(tx, ctx)
    return a


@op('cap.status', 'operation:str recovery?:bool', False)
def status(tx, ctx, operation, recovery=False):
    """Read an accepted submission while waiting, then its action and provider result."""
    a = tx.get('actions', operation)
    if not a and recovery:
        from .completion_receipts import find
        a = find(tx, operation)
        if not a:
            return dict(id=operation, state='missing_unknown', acknowledged=False,
                        result_expired=True, replay_allowed=False,
                        reason='No retained authoritative completion; reconcile original executor evidence')
    if not a:
        # A combined submission can wait for capacity before any action exists.
        # Preserve ordinary missing-action semantics after lease retention.
        submission = tx.get('_submissions', operation)
        if submission and submission.get('lease'):
            lease = tx.get('leases', submission['lease'])
            if lease and lease['state'] != 'released':
                return submission_status(tx, ctx, operation)
        a = tx.need('actions', operation)
    from .retention import require_payload
    owner(ctx, a['provider'])
    if not recovery:
        require_payload(a)
    return a


@op('cap.leases', 'task?:str', False)
def leases(tx, ctx, task=''):
    return [l for l in tx.rows('leases') if not task or l['task'] == task]


def start_submission(tx, ctx, lease):
    operation = tx.get('_submission_leases', lease['id'])
    if not operation or lease['state'] not in ('bound', 'fencing'):
        return
    submission = tx.need('_submissions', operation)
    dispatch(tx, ctx, lease['id'], submission['inputs'], operation, submission.get('destination', ''))
    finish_submission(tx, ctx, operation)


def finish_submission(tx, ctx, operation):
    submission = tx.get('_submissions', operation)
    action = tx.get('actions', operation)
    if submission and action is None:
        lease = tx.get('leases', submission['lease'])
        if not lease or lease['state'] == 'released':
            tx.drop('_submission_active', operation)
        return
    if submission and action and action['state'] in lifecycle.TERMINAL:
        held = tx.get('leases', submission['lease'])
        if held:
            release(tx, held)
        if tx.get('_submission_active', operation):
            tx.drop('_submission_active', operation)


@op('cap.submit', 'contract:str inputs:obj operation:str resource?:str destination?:str placement?:obj')
def submit(tx, ctx, contract, resource='', inputs=None, operation='', destination='', placement=None, maintenance_deployment=''):
    """Durably acquire, fence, dispatch and release one command; safe across disconnects."""
    if ctx.permissions is not None:
        raise Fault('Submission requires task owner access', 403)
    ident(operation)
    validate(tx.need('contracts', contract)['inputs'], inputs)
    resource, placement = selection.normalize(resource, placement)
    lease_id = selection.identity(operation, contract, resource, placement)
    record = dict(id=operation, lease=lease_id, contract=contract, resource=resource,
                  inputs=inputs, owner=ctx.actor)
    if maintenance_deployment:
        record['maintenance_deployment'] = maintenance_deployment
    if placement:
        record['placement'] = placement
    if destination:
        lifecycle.destination(tx, ctx, destination)
        record['destination'] = destination
    prior = tx.get('_submissions', operation)
    if prior:
        from .retention import require_payload
        require_payload(prior)
    if prior and prior != record:
        raise Fault('Submission identity conflict', 409)
    action = tx.get('actions', operation)
    if action:
        from .retention import require_payload
        require_payload(action)
        if (action['lease'], action['contract'], action['inputs'], action.get('destination', '')) != (lease_id, contract, inputs, destination):
            raise Fault('Operation identity conflict', 409)
        # Adopt an existing Android operation without replaying it or re-leasing.
        tx.put('_submissions', operation, record)
        finish_submission(tx, ctx, operation)
        return action
    from .completion_receipts import find
    if find(tx, operation):
        raise Fault('Operation result expired; inspect the retained completion receipt, do not replay', 410)
    if prior:
        original_lease = tx.get('leases', lease_id)
        if not original_lease or original_lease['state'] == 'released':
            # History alone is not evidence that the original command never ran.
            return submission_status(tx, ctx, operation)
    tx.put('_submissions', operation, record)
    tx.put('_submission_leases', lease_id, operation)
    tx.put('_submission_active', operation, {'id': operation})
    lease = acquire(tx, ctx, operation, contract, resource, placement=placement,
                    maintenance_deployment=maintenance_deployment)
    start_submission(tx, ctx, lease)
    return submission_status(tx, ctx, operation)


@op('cap.submission', 'operation:str', False)
def submission_status(tx, ctx, operation):
    """Read a combined request while queued, fencing, executing or completed."""
    submission = tx.need('_submissions', operation)
    if ctx.permissions is not None and submission['owner'] != ctx.actor:
        raise Fault('Other submission', 403)
    from .retention import require_payload
    require_payload(submission)
    action = tx.get('actions', operation)
    if action:
        return require_payload(action)
    lease = tx.get('leases', submission['lease'])
    if not lease or lease['state'] == 'released':
        return dict(id=operation, lease=submission['lease'], state='missing_unknown',
                    acknowledged=False, replay_allowed=False,
                    reason='Original outcome unavailable; reconcile executor evidence')
    return dict(id=operation, lease=lease['id'], state=lease['state'])


def settle_submissions(tx, ctx):
    # Routine-backed commands finish outside provider.result. Tick releases only
    # completed combined requests; uncertain commands keep their original fence.
    for submission in tx.rows('_submission_active'):
        finish_submission(tx, ctx, submission['id'])
    if recover_reservations(tx, ctx):
        promote(tx, ctx)



# Register explicit ownership and proof-backed execution reconciliation APIs.
from . import resource_recovery
