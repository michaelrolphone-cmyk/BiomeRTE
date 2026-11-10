"""Bounded discrete capability lifetimes. No stream messages enter this store."""
import math
import time
from .api import op, ident
from .store import Fault, digest

TERMINAL = ('succeeded', 'failed', 'abandoned')
RETENTION = 900


def configuration(value, builtin=False):
    value = dict(value or {})
    cls = value.setdefault('lease_class', 'persistent' if builtin else 'command')
    if cls not in ('persistent', 'command', 'long_command'):
        raise Fault('Invalid lease class')
    for name, default, maximum in (
            ('fence_ttl_seconds', 120, 900), ('watch_ttl_seconds', 120, 900),
            ('heartbeat_interval_seconds', 60, 300), ('missed_heartbeats_limit', 3, 10)):
        n = value.setdefault(name, default)
        if isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) or not 0 < n <= maximum:
            raise Fault('Invalid ' + name)
    if type(value['missed_heartbeats_limit']) is not int:
        raise Fault('Missed heartbeats limit must be integer')
    policy = dict(value.get('reservation_policy', {}))
    policy.setdefault('release_after_result', cls != 'persistent')
    value['reservation_policy'] = policy
    return value


def policy(tx, lease):
    return configuration(tx.get('offers', lease.get('offer', ''), {}).get('configuration'),
                         lease.get('provider') in ('builtin', 'routine'))


def current(tx, action):
    lease = tx.need('leases', action['lease'])
    guard = tx.need('guards', lease['guard'])
    if (action['state'] == 'abandoned' or
            (guard['holder'], guard['generation']) != (lease['id'], action['generation'])):
        raise Fault('Superseded generation', 409)
    return lease


def destination(tx, ctx, id):
    row = tx.need('destinations', id)
    if row['state'] != 'active' or row['expires_at'] <= time.time():
        raise Fault('Destination expired', 403)
    if ctx.permissions is not None and row['owner'] != ctx.actor:
        raise Fault('Other destination', 403)
    return row


@op('destination.register', 'id:str kind:str inbox:str ttl_seconds:int retention_seconds?:int')
def register(tx, ctx, id, kind, inbox, ttl_seconds, retention_seconds=3600):
    ident(id)
    ident(inbox)
    if not 0 < ttl_seconds <= 86400 or not 0 < retention_seconds <= 21600:
        raise Fault('Invalid destination lifetime')
    if ctx.permissions is not None:
        peer = tx.need('peers', ctx.actor)
        if not peer['enabled'] or 'destination.register' not in peer['scopes']:
            raise Fault('Enrolled controller required', 403)
    old = tx.get('destinations', id)
    if old and old['owner'] != ctx.actor:
        raise Fault('Destination belongs to another identity', 403)
    return tx.put('destinations', id, dict(id=id, kind=ident(kind), inbox=inbox,
        owner=ctx.actor, ttl_seconds=ttl_seconds, retention_seconds=retention_seconds,
        expires_at=time.time() + ttl_seconds, state='active'))


@op('destination.revoke', 'id:str')
def revoke(tx, ctx, id):
    row = destination(tx, ctx, id)
    row.update(state='revoked', expires_at=time.time())
    tx.put('destinations', id, row)
    for entry in tx.rows('result_inbox'):
        if entry['destination'] == id:
            tx.drop('result_inbox', entry['id'])
    return row


@op('destination.list', write=False)
def registrations(tx, ctx):
    return [r for r in tx.rows('destinations') if
            (ctx.permissions is None or r['owner'] == ctx.actor) and
            r['state'] == 'active' and r['expires_at'] > time.time()]


@op('inbox.list', 'destination:str', False)
def inbox(tx, ctx, destination):
    globals()['destination'](tx, ctx, destination)
    return [r for r in tx.rows('result_inbox') if
            r['destination'] == destination and r['expires_at'] > time.time()]


@op('inbox.ack', 'destination:str operation:str')
def ack(tx, ctx, destination, operation):
    globals()['destination'](tx, ctx, destination)
    tx.drop('result_inbox', digest([destination, operation]))
    return {'acknowledged': operation}


def deliver(tx, action):
    if not action.get('destination') or action['state'] not in TERMINAL:
        return
    row = tx.get('destinations', action['destination'])
    now = time.time()
    if not row or row['state'] != 'active' or row['expires_at'] <= now:
        return
    key = digest([row['id'], action['id']])
    # ACK deletes the inbox entry, not this marker, so recovery cannot redeliver it.
    if action.get('destination_delivered'):
        return
    tx.put('result_inbox', key, dict(id=key, destination=row['id'], operation=action['id'],
        state=action['state'], result=action.get('result', {}), delivered_at=now,
        expires_at=min(now + row['retention_seconds'], row['expires_at'])))
    action['destination_delivered'] = True
    tx.put('actions', action['id'], action)


def settled(tx, action, now=None):
    if action['state'] not in TERMINAL:
        return
    if not action.get('expires_at'):
        action['expires_at'] = (time.time() if now is None else now) + RETENTION
        tx.put('actions', action['id'], action)
    deliver(tx, action)


def abandon(tx, ctx, lease, reason, now):
    from .providers import promote
    if lease['state'] == 'released':
        return False
    guard = tx.need('guards', lease['guard'])
    if (guard['holder'], guard['generation']) != (lease['id'], lease['generation']):
        return False
    guard.update(generation=guard['generation'] + 1, holder='', released_at=now)
    tx.put('guards', lease['guard'], guard)
    # A compact watermark prevents a swept guard resetting physical fence numbers.
    tx.put('_fence_generations', lease['guard'], guard['generation'])
    for action in tx.rows('actions'):
        if action['lease'] != lease['id'] or action['generation'] != lease['generation'] or action['state'] in TERMINAL:
            continue
        action.update(state='abandoned', abandoned_generation=lease['generation'],
                      result={'reason': reason, 'effects': 'unknown_reconcile_before_replay'})
        tx.put('actions', action['id'], action)
        from .authorization import observe
        observe(tx, action['id'], 'abandoned', action['result'])
        tx.emit('operation.abandoned', {'id': action['id'], 'reason': reason})
        settled(tx, action, now)
    lease.update(state='released', released_at=now, expires_at=now + 300)
    tx.put('leases', lease['id'], lease)
    operation = tx.get('_submission_leases', lease['id'])
    if operation:
        tx.drop('_submission_active', operation)
    tx.emit('lease.abandoned', {'lease': lease['id'], 'generation': lease['generation'], 'reason': reason})
    return True


@op('cap.heartbeat', 'provider:str operation:str generation:int')
def heartbeat(tx, ctx, provider, operation, generation):
    from .providers import executor
    executor(tx, ctx, provider)
    action = tx.need('actions', operation)
    if (action['provider'], action['generation']) != (provider, generation):
        raise Fault('Progress binding mismatch', 409)
    lease = current(tx, action)
    if action['state'] not in ('claimed', 'uncertain'):
        raise Fault('Progress requires claimed work', 409)
    now = time.time()
    cfg = policy(tx, lease)
    action['last_progress_at'] = now
    if cfg['lease_class'] == 'command':
        action['watch_until'] = now + cfg['watch_ttl_seconds']
    lease.update(last_progress_at=now, missed_heartbeats=0)
    tx.put('actions', operation, action)
    tx.put('leases', lease['id'], lease)
    return {'operation': operation, 'last_progress_at': now}


def recover(tx, ctx, lease, actions, now):
    cfg = policy(tx, lease)
    changed = False
    if 'lease_class' not in lease:
        lease.update(lease_class=cfg['lease_class'], fencing_since=now,
                     last_progress_at=now, missed_heartbeats=0)
        changed = True
    if lease['state'] == 'fencing':
        # Only cap.fenced establishes custody. An unrelated heartbeat cannot
        # indefinitely extend an unacknowledged fence.
        if now >= lease.get('fencing_since', now) + cfg['fence_ttl_seconds']:
            return abandon(tx, ctx, lease, 'fence_timeout', now)
    elif cfg['lease_class'] != 'persistent':
        for action in actions:
            if action['state'] not in ('claimed', 'uncertain'):
                continue
            if 'last_progress_at' not in action:
                action.update(last_progress_at=now, watch_until=now + cfg['watch_ttl_seconds'])
                tx.put('actions', action['id'], action)
            if cfg['lease_class'] == 'command':
                if now >= action['watch_until']:
                    return abandon(tx, ctx, lease, 'command_watch_timeout', now)
            else:
                missed = int(max(0, now - action['last_progress_at']) // cfg['heartbeat_interval_seconds'])
                if missed >= cfg['missed_heartbeats_limit']:
                    return abandon(tx, ctx, lease, 'missed_heartbeats', now)
                if lease.get('missed_heartbeats') != missed:
                    lease['missed_heartbeats'] = missed
                    changed = True
    if changed:
        tx.put('leases', lease['id'], lease)
    return False


def sweep(tx, ctx, now):
    # Only capability-path events expire; workflow/knowledge history is untouched.
    for kind in ('result_inbox', 'destinations'):
        for row in tx.rows(kind):
            if row['expires_at'] <= now:
                tx.drop(kind, row['id'])
    active_destinations = {r['id'] for r in tx.rows('destinations')}
    for row in tx.rows('result_inbox'):
        if row['destination'] not in active_destinations:
            tx.drop('result_inbox', row['id'])
    referenced = {t.get('action') for run in tx.rows('runs') if run['state'] not in ('completed', 'failed', 'cancelled')
                  for t in run.get('tokens', [])}
    for action in tx.rows('actions'):
        settled(tx, action, now)
        if (action.get('expires_at') or now + 1) <= now and action['id'] not in referenced:
            tx.drop('actions', action['id'])
            for kind in ('_submissions', '_submission_active'):
                tx.drop(kind, action['id'])
    action_leases = {a['lease'] for a in tx.rows('actions')}
    for lease in tx.rows('leases'):
        if lease['state'] == 'released' and lease.get('expires_at', now + 1) <= now and lease['id'] not in action_leases:
            for kind in ('leases', '_reservation_activity', '_submission_leases'):
                tx.drop(kind, lease['id'])
    for row in tx.db.execute("SELECT id,value FROM records WHERE kind='guards'").fetchall():
        import json
        guard = json.loads(row[1])
        if not guard['holder'] and guard.get('released_at', now + 1) + 300 <= now:
            tx.put('_fence_generations', row[0], max(guard['generation'], tx.get('_fence_generations', row[0], 0)))
            tx.drop('guards', row[0])
    for row in tx.db.execute("SELECT id,value FROM records WHERE kind='_receipt'").fetchall():
        import json
        if json.loads(row[1]).get('expires_at', now + 1) <= now:
            tx.drop('_receipt', row[0])
    for event in tx.rows('events'):
        if event.get('expires_at', now + 1) <= now:
            tx.drop('events', '%020d' % event['seq'])
