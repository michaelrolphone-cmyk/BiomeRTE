"""Generic stream subscriptions sharing one provider reservation and fence."""
import time
from .store import Fault
from . import lifecycle

def enabled(offer):
    return offer.get('configuration', {}).get('stream', {}).get('enabled') is True

def root(tx, lease):
    return tx.need('leases', lease.get('shared_parent', lease['id']))

def members(tx, anchor):
    values = [anchor] + [l for l in tx.matching('leases', ('bound', 'fencing'))
                        if l.get('shared_parent') == anchor['id']]
    return [l for l in values if l['state'] in ('bound', 'fencing')
            and not l.get('shared_owner_released')]

def join(tx, ctx, request, offer):
    if not enabled(offer):
        return None
    anchors = [l for l in tx.matching('leases', ('bound', 'fencing'))
               if l.get('offer') == offer['id'] and not l.get('shared_parent')]
    if not anchors:
        return None
    anchor = anchors[0]
    value = dict(request, offer=offer['id'], provider=anchor['provider'],
                 resource=anchor['resource'], generation=anchor['generation'],
                 guard=anchor['guard'], state='bound', shared_parent=anchor['id'],
                 lease_class='persistent', lease_policy=lifecycle.policy(tx, anchor),
                 last_progress_at=time.time(), missed_heartbeats=0)
    if value.get('queue_key'):
        tx.drop('_lease_queue', value.pop('queue_key'))
    tx.put('leases', value['id'], value)
    tx.emit('lease.shared', {'lease': value['id'], 'parent': anchor['id'],
                            'contract': value['contract'], 'resource': value['resource']})
    return value

def release(tx, lease):
    """Return None for ordinary release, True for subscription-only release."""
    if lease.get('shared_parent'):
        anchor = root(tx, lease)
        lease.update(state='released', released_at=time.time(), expires_at=time.time()+300)
        tx.put('leases', lease['id'], lease)
        if anchor.get('shared_owner_released') and not members(tx, anchor):
            from .providers import release as release_anchor
            anchor.pop('shared_owner_released', None)
            release_anchor(tx, anchor)
        return True
    offer = tx.get('offers', lease.get('offer', ''), {})
    if enabled(offer) and any(l.get('shared_parent') == lease['id']
                             for l in members(tx, lease)):
        lease['shared_owner_released'] = True
        tx.put('leases', lease['id'], lease)
        return True
    return None

def recover(tx, lease):
    if not lease.get('shared_parent'):
        return False
    anchor = tx.get('leases', lease['shared_parent'], {})
    if anchor.get('state') not in ('bound', 'fencing'):
        lease.update(state='released', released_at=time.time(), expires_at=time.time()+300)
        tx.put('leases', lease['id'], lease)
    return True

def consumers(tx, grant):
    anchor = tx.need('leases', grant['lease'])
    closed = grant.get('closed_consumers', [])
    return [l for l in members(tx, anchor) if l['id'] not in closed]

def consumer(tx, ctx, grant, lease=None):
    values = consumers(tx, grant)
    if lease is not None:
        values = [l for l in values if l['id'] == lease['id']]
    if ctx.permissions is not None:
        values = [l for l in values if l.get('owner') == ctx.actor]
    if not values:
        raise Fault('Acquire a stream subscription before attaching', 403)
    return values[0]

def public(tx, grant):
    return dict(grant, consumers=[l['id'] for l in consumers(tx, grant)])
