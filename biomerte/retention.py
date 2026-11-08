"""Opt-in rolling debug payload retention, never a historical data migration.

Only records created inside a tracked application transaction are enrolled.
Identity fences remain durable; their payloads explicitly become unavailable.
Unknown record kinds are roots, so recovery and user knowledge fail closed.
"""
import contextlib
import json
import time
from .store import Fault, digest

KINDS = {'runs', 'actions', 'events', 'artifacts', '_receipt', '_submissions'}
TERMINAL = {'runs': {'completed', 'failed', 'cancelled'},
            'actions': {'succeeded', 'failed'}}
META = 'operational_retention'


def strings(value):
    if isinstance(value, str):
        yield value
        if value.startswith('action:'):
            yield value[7:]
    elif isinstance(value, dict):
        for key, item in value.items():
            # Values and map keys may both contain artifact/run references.
            yield key
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


@contextlib.contextmanager
def tracking(tx, now=None):
    """Track future writes only; no backfill and no mutation of business values."""
    now = time.time() if now is None else now
    original = tx.put
    if tx.get('_meta', META) is None:
        original('_meta', META, {'version': 1, 'enabled_at': now, 'event_floor': 0})

    def put(kind, key, value):
        if kind in KINDS and (kind != '_receipt' or value.get('format') == 2):
            marker_key = kind + ':' + key
            marker = tx.get('_retention', marker_key)
            if marker is None and tx.get(kind, key) is None:
                marker = {'id': marker_key, 'kind': kind, 'key': key, 'born': now}
            if marker is not None:
                terminal = kind in ('events', 'artifacts', '_receipt') or value.get('state') in TERMINAL.get(kind, ())
                previous = dict(marker)
                if terminal:
                    marker.setdefault('terminal_at', now)
                else:
                    marker.pop('terminal_at', None)
                if previous != marker or tx.get('_retention', marker_key) is None:
                    original('_retention', marker_key, marker)
        return original(kind, key, value)
    tx.put = put
    try:
        yield
    finally:
        tx.put = original


def compact_action(value):
    """Keep exact effect/result identities and minimal quiescence evidence.

    The hashes permit a disconnected worker to reconcile or redeliver the exact
    result after output expiry without resurrecting bytes or repeating effects.
    They are computed before destroying inputs/results, never from a summary.
    """
    summary = {field: value[field] for field in (
        'id', 'state', 'provider', 'lease', 'contract', 'resource', 'generation', 'guard')
        if field in value}
    summary['identity_digest'] = digest([value['contract'], value['inputs'],
                                        value['resource'], value['generation'], value['lease']])
    result = value.get('result', {})
    summary['result_digest'] = digest(result)
    recovery = {'result_expired': True, 'result_digest': summary['result_digest']}
    if isinstance(result, dict):
        for field in ('reconciliation', 'evidence'):
            if isinstance(result.get(field), dict) and result[field].get('quiescent') is True:
                recovery[field] = {'quiescent': True}
    summary['recovery_result'] = recovery
    return summary


def require_payload(record):
    if record.get('result_expired'):
        raise Fault('Operational result expired; identity remains reserved', 410)
    return record


def check_event_cursor(tx, cursor):
    floor = tx.get('_meta', META, {}).get('event_floor', 0)
    if cursor < floor:
        raise Fault('Event history expired; resynchronize from a snapshot (event floor %s)' % floor, 410)


def sweep(tx, now=None, seconds=86400, keep=1024, batch=128, index_ready=None):
    """Expire eligible payloads by elapsed time or rolling count, preserving roots.

    ``keep`` counts completed records per kind. Active/referenced records are
    exempt; this is not an absolute database size limit. Identity skeletons are
    retained because the legacy operation API has no expiring ID contract.
    """
    now = time.time() if now is None else now
    if seconds < 0 or keep < 0 or batch < 1:
        raise ValueError('Invalid operational retention policy')
    from .retention_index import advance
    if index_ready is None:
        index_ready = advance(tx.db)
    if not index_ready:
        return 0
    # Persist a local keyset cursor, so pinned/oversized oldest records cannot
    # starve later work. This is disposable scheduling state, not journal data.
    cursor = tx.db.execute('SELECT marker FROM retention_candidate_cursor WHERE id=1').fetchone()[0]
    marker_rows = tx.db.execute("SELECT id,value FROM records WHERE kind='_retention' AND id>? ORDER BY id LIMIT ?",
                                (cursor, min(128, max(batch, 64)))).fetchall()
    if not marker_rows:
        marker_rows = tx.db.execute("SELECT id,value FROM records WHERE kind='_retention' ORDER BY id LIMIT ?",
                                    (min(128, max(batch, 64)),)).fetchall()
    markers, records, used = [], {}, 0
    payload_budget = 1048576
    for marker_id, encoded in marker_rows:
        marker = json.loads(encoded)
        key = (marker['kind'], marker['key'])
        size = tx.db.execute('SELECT length(CAST(value AS BLOB)) FROM records WHERE kind=? AND id=?', key).fetchone()
        size = size[0] if size else 0
        if size <= payload_budget and used + size > payload_budget:
            break  # Resume here next tick, rather than repeatedly skipping it.
        tx.db.execute('UPDATE retention_candidate_cursor SET marker=? WHERE id=1', (marker_id,))
        if size > payload_budget:
            continue  # Conservatively retain exceptional oversized payloads.
        value = tx.get(*key)
        used += size
        if value is not None:
            records[key] = value
            markers.append(marker)
    for m in markers:
        if m['kind'] == '_submissions' and 'terminal_at' not in m:
            # Scalar projection avoids decoding a possibly large action result.
            row = tx.db.execute("SELECT value FROM record_lookup WHERE kind='actions' AND id=? AND field='state' AND NOT EXISTS (SELECT 1 FROM record_lookup_dirty WHERE kind='actions' AND id=record_lookup.id)",
                                (m['key'],)).fetchone()
            if row and json.loads(row[0]) in TERMINAL['actions'] and tx.get('_submission_active', m['key']) is None:
                m['terminal_at'] = now
                tx.put('_retention', m['id'], m)
    # Count policy uses compact marker metadata only, never operational values.
    thresholds = {}
    if keep:
        for kind in KINDS:
            row = tx.db.execute("SELECT terminal,key FROM retention_markers WHERE kind=? "
                                "AND terminal IS NOT NULL ORDER BY terminal DESC,key DESC LIMIT 1 OFFSET ?",
                                (kind, keep - 1)).fetchone()
            if row:
                thresholds[kind] = row
    candidates = {}
    for kind in KINDS:
        ready = sorted((m for m in markers if m['kind'] == kind and 'terminal_at' in m),
                       key=lambda m: (m['terminal_at'], m['key']))
        for index, m in enumerate(ready):
            key = (kind, m['key'])
            if key in records and (now - m['terminal_at'] >= seconds or keep == 0 or
                    kind in thresholds and (m['terminal_at'], m['key']) < thresholds[kind]):
                if kind == 'actions' and any(field not in records[key] for field in
                        ('id', 'state', 'provider', 'contract', 'inputs', 'resource', 'generation', 'lease')):
                    continue  # An incomplete identity cannot safely become a replay fence.
                candidates[key] = m
    # Only explicitly transient output chunks may expire. Durable uploads
    # always win when content-addressed bytes are shared.
    for key, m in list(candidates.items()):
        if key[0] == 'artifacts' and (not m.get('operational') or m.get('durable')):
            del candidates[key]
    # Preserve unread internal process triggers and pending assistant wake input.
    cursors = [r['cursor'] for r in tx.rows('subscriptions') if r.get('process')]
    cursors.extend(r.get('cursor', 0) for r in tx.rows('assistant_wake_outbox'))
    if cursors:
        for key in list(candidates):
            if key[0] == 'events' and records[key]['seq'] > min(cursors):
                del candidates[key]
    if not candidates:
        return 0
    # Fixed-point graph reachability. Never let a batch split a reference
    # dependency by assuming a non-selected owner will also be compacted.
    from .retention_index import protected_by_roots as indexed_roots
    def protected_by_roots(pool):
        return indexed_roots(tx, pool, records)
    protected = protected_by_roots(candidates)
    eligible = {key: marker for key, marker in candidates.items() if key not in protected}
    priority = {'_receipt': 0, '_submissions': 1, 'runs': 2, 'actions': 3, 'events': 4, 'artifacts': 5}
    candidates = dict(sorted(eligible.items(), key=lambda item:
                           (priority[item[0][0]], item[1]['terminal_at'], item[0]))[:batch])
    protected = protected_by_roots(candidates)
    changed = 0
    meta = tx.get('_meta', META, {})
    floor = meta.get('event_floor', 0)
    for key, marker in sorted(candidates.items(), key=lambda item: (item[1]['terminal_at'], item[0])):
        if changed >= batch:
            break
        if key in protected:
            continue
        kind, ident = key
        value = records[key]
        if kind in ('_receipt', '_submissions'):
            fields = ('signature', 'format', 'created_at', 'operation') if kind == '_receipt' else ('id', 'lease', 'contract', 'resource', 'owner')
            summary = {field: value[field] for field in fields if field in value}
            summary.update(result_expired=True, expired_at=now)
            tx.put(kind, ident, summary)
        elif kind in ('runs', 'actions'):
            fields = ('id', 'name', 'task', 'parent', 'state', 'steps', 'rev') if kind == 'runs' else (
                'id', 'state', 'provider', 'lease', 'contract', 'resource', 'generation', 'guard')
            summary = compact_action(value) if kind == 'actions' else {field: value[field] for field in fields if field in value}
            summary.update(result_expired=True, expired_at=now)
            tx.put(kind, ident, summary)
        else:
            tx.drop(kind, ident)
            if kind == 'events':
                floor = max(floor, value['seq'])
        tx.drop('_retention', marker['id'])
        changed += 1
    if floor != meta.get('event_floor', 0):
        meta['event_floor'] = floor
        tx.put('_meta', META, meta)
    return changed
