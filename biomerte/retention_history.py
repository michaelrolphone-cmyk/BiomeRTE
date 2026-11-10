"""Bounded, explicit historical enrollment. Never retires payloads itself.

Event sequence is completion order, NOT a timestamp. Missing provenance remains
an explicit exception; neither wall-clock age nor SQLite rowid is invented.
"""
import json
import math
from .store import Fault, digest
from .retention import KINDS, TERMINAL

CURSOR = 'operational_retention_history_v1'
MAX_ROWS = 64
MAX_BYTES = 1048576


def event_targets(event):
    seq = event.get('seq')
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        return []
    topic, payload = event.get('topic'), event.get('payload', {})
    targets = [('events', '%020d' % seq)]
    if not isinstance(payload, dict):
        return targets
    if topic in ('operation.succeeded', 'operation.failed') and isinstance(payload.get('id'), str):
        targets += [('actions', payload['id']), ('_submissions', payload['id'])]
    elif topic in ('process.completed', 'process.failed', 'process.cancelled') and isinstance(payload.get('run'), str):
        targets.append(('runs', payload['run']))
    return targets


def plan(tx, batch=MAX_ROWS):
    """Dry run one resumable page, returning only identities/hashes/reasons.

    Execute via Store read_only=True. Exceptions are page results, not a growing
    canonical log. Cursor progress commits only with apply of this exact plan.
    """
    if not isinstance(batch, int) or isinstance(batch, bool) or not 1 <= batch <= MAX_ROWS:
        raise ValueError('Historical enrollment batch must be 1..64')
    cursor = tx.get('_meta', CURSOR, {'phase': 'events', 'key': '', 'kind': ''})
    phase = cursor['phase']
    result = {'version': 1, 'cursor': cursor, 'next': dict(cursor), 'batch': batch,
              'reads': [], 'markers': [], 'exceptions': [], 'bytes': 0, 'scanned': 0}
    cache = {}
    def read(kind, key):
        identity = (kind, key)
        if identity in cache:
            return cache[identity]
        row = tx.db.execute('SELECT length(CAST(value AS BLOB)) FROM records WHERE kind=? AND id=?', identity).fetchone()
        if row and (row[0] > MAX_BYTES or result['bytes'] + row[0] > MAX_BYTES):
            raise OverflowError('payload_budget')
        value = tx.get(kind, key)
        result['bytes'] += row[0] if row else 0
        result['reads'].append([kind, key, digest(value)])
        cache[identity] = value
        return value
    def enroll(kind, key, order=None, event=None):
        value = read(kind, key)
        if value is None or not isinstance(value, dict):
            return 'missing_or_nonobject'
        if value.get('result_expired'):
            return 'already_retired'
        marker_key = kind + ':' + key
        if len(marker_key) > 512:
            return 'marker_identity_exceeds_bound'
        marker = read('_retention', marker_key)
        if kind in ('actions', 'runs', 'leases') and value.get('state') not in TERMINAL[kind]:
            return 'active_or_uncertain'
        if event is not None and kind in ('actions', 'runs'):
            if event['topic'].split('.', 1)[1] != value.get('state'):
                return 'completion_state_changed'
        if kind == 'actions':
            if any(f not in value for f in ('id','state','provider','contract','inputs','resource','generation','lease')):
                return 'incomplete_action_identity'
            outcome = value.get('result', {})
            if isinstance(outcome, dict) and any(isinstance(outcome.get(f), dict) and
                    outcome[f].get('quiescent') is False for f in ('evidence','reconciliation')):
                return 'nonquiescent_result'
        if kind == '_submissions':
            action = read('actions', key)
            if not action or action.get('state') not in TERMINAL['actions'] or read('_submission_active', key) is not None:
                return 'active_or_uncertain_submission'
        if kind == 'artifacts':
            # Historical artifact bodies have no trustworthy transient flag.
            if not marker or not marker.get('operational') or marker.get('durable'):
                return 'artifact_intent_unproven_or_durable'
        new = dict(marker or {'id': marker_key, 'kind': kind, 'key': key, 'historical': True})
        if kind=='recovery_compactions' and order is None:
            previous=value.get('previous_head')
            if type(previous) is not int or previous<0 or value.get('request_id')!=key or not isinstance(value.get('removed'),list):return 'recovery_completion_order_unavailable'
            order=previous+1
        if order is not None:
            new['completed_order'] = max(order, new.get('completed_order', 0))
        elif kind == '_receipt':
            if not isinstance(value.get('signature'), str) or 'result' not in value:
                return 'incomplete_receipt_identity'
            # Only format 2 defines this server-authored timestamp contract.
            stamp = value.get('created_at')
            if (value.get('format') != 2 or isinstance(stamp, bool) or
                    not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or stamp < 0):
                return 'receipt_original_age_order_unavailable'
            new.setdefault('terminal_at', stamp)
        elif not marker or not ('terminal_at' in marker or 'completed_order' in marker):
            return 'original_completion_evidence_unavailable'
        if new != marker:
            result['markers'].append(new)
        return None
    if phase == 'done':
        return result
    if phase == 'events':
        rows = tx.db.execute("SELECT id FROM records WHERE kind='events' AND id>? ORDER BY id LIMIT ?",
                             (cursor['key'], batch)).fetchall()
    else:
        current_kind = cursor['kind']
        if not current_kind:
            row = tx.db.execute('SELECT kind FROM records ORDER BY kind,id LIMIT 1').fetchone()
            current_kind = row[0] if row else ''
        rows = tx.db.execute('SELECT kind,id FROM records WHERE kind=? AND id>? ORDER BY id LIMIT ?',
                             (current_kind, cursor['key'], batch)).fetchall()
    for row in rows:
        kind, key = ('events', row[0]) if phase == 'events' else row
        saved = len(result['markers'])
        try:
            if phase == 'events':
                event = read(kind, key)
                if not isinstance(event, dict) or not event_targets(event) or key != '%020d' % event['seq']:
                    result['exceptions'].append([kind, key, 'invalid_event_sequence'])
                else:
                    for target_kind, target_key in event_targets(event):
                        reason = enroll(target_kind, target_key, event['seq'], event)
                        if reason:
                            result['exceptions'].append([target_kind, target_key, reason])
            elif kind in KINDS:
                reason = enroll(kind, key)
                if reason:
                    result['exceptions'].append([kind, key, reason])
        except OverflowError:
            del result['markers'][saved:]
            result['exceptions'].append([kind, key, 'payload_budget'])
            # The cursor skips this explicit exception; a later explicit reset
            # or separately bounded evidence resolution can revisit it.
        result['scanned'] += 1
        result['next'] = {'phase': phase, 'kind': kind if phase != 'events' else '', 'key': key}
    if len(rows) < batch:
        if phase == 'events':
            result['next'] = {'phase': 'records', 'kind': '', 'key': ''}
        else:
            row = tx.db.execute('SELECT kind FROM records WHERE kind>? ORDER BY kind,id LIMIT 1',
                                (current_kind,)).fetchone()
            result['next'] = {'phase': 'records' if row else 'done', 'kind': row[0] if row else '', 'key': ''}
    # Multiple terminal events for one record in a page: retain latest evidence.
    merged = {}
    for marker in result['markers']:
        ident = marker['id']
        if ident in merged and 'completed_order' in marker:
            marker['completed_order'] = max(marker['completed_order'], merged[ident].get('completed_order', 0))
        merged[ident] = marker
    result['markers'] = [merged[k] for k in sorted(merged)]
    return result


def apply(tx, approved_plan):
    """Recompute the bounded page in the same transaction, then enroll atomically.

    No payload deletion, arbitrary marker injection, timestamp reset, or DDL.
    Caller must explicitly authorize a later ordinary sweep separately.
    """
    current = plan(tx, approved_plan.get('batch', MAX_ROWS))
    if digest(current) != digest(approved_plan):
        raise Fault('Historical retention dry-run preimage changed', 409)
    for marker in current['markers']:
        tx.put('_retention', marker['id'], marker)
    tx.put('_meta', CURSOR, current['next'])
    return {'enrolled': len(current['markers']), 'scanned': current['scanned'],
            'exceptions': current['exceptions'], 'next': current['next']}
