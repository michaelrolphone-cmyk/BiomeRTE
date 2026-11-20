"""Opt-in rolling debug payload retention with explicit historical enrollment.

Ordinary tracking enrolls new records; retention_history handles reviewed backfill.
Completed records expire in full; active and unresolved records remain protected.
Unknown record kinds are roots, so recovery and user knowledge fail closed.
"""
import contextlib
import json
import time
from .store import Fault, digest

KINDS = {'runs', 'actions', 'events', 'artifacts', '_receipt', '_submissions', 'recovery_compactions', 'leases'}
TERMINAL = {'runs': {'completed', 'failed', 'cancelled'},
            'actions': {'succeeded', 'failed'}, 'leases': {'released'}}
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
            created = marker is None and tx.get(kind, key) is None
            if created:
                marker = {'id': marker_key, 'kind': kind, 'key': key, 'born': now}
            if marker is not None:
                terminal = kind in ('events', 'artifacts', '_receipt', 'recovery_compactions') or value.get('state') in TERMINAL.get(kind, ())
                previous = dict(marker)
                revision=getattr(tx,'current_revision',getattr(tx,'journal_sequence',None))
                if kind == '_receipt' and created and revision is not None:
                    marker.setdefault('completed_order', revision)
                    marker.setdefault('order_source', 'current_revision' if hasattr(tx,'current_revision') else 'journal')
                if terminal:
                    marker.setdefault('terminal_at', now)
                else:
                    marker.pop('terminal_at', None)
                    marker.pop('completed_order', None)
                    marker.pop('observed_before', None)
                    marker.pop('observation_digest', None)
                if previous != marker or tx.get('_retention', marker_key) is None:
                    original('_retention', marker_key, marker)
        result = original(kind, key, value)
        if kind == 'events':
            from .retention_history import event_targets
            for target_kind, target_key in event_targets(value):
                marker = tx.get('_retention', target_kind + ':' + target_key)
                if marker is not None:
                    marker['completed_order'] = value['seq']
                    original('_retention', marker['id'], marker)
        return result
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


def terminal_safe(kind,value):
    if kind not in TERMINAL:return True
    if value.get('state') not in TERMINAL[kind]:return False
    for outcome in (value.get('result',{}),value.get('recovery_result',{})):
        if isinstance(outcome,dict) and any(isinstance(outcome.get(k),dict) and outcome[k].get('quiescent') is False for k in ('evidence','reconciliation')):return False
    return True


def window_expired(marker, now, seconds, keep, thresholds, order_thresholds):
    """Pure rolling time/count predicate shared by sweep and offline selection."""
    kind, key = marker['kind'], marker['key']
    return bool(
        'terminal_at' in marker and now - marker['terminal_at'] >= seconds or
        'observed_before' in marker and now - marker['observed_before'] >= seconds or
        keep == 0 or
        'terminal_at' in marker and kind in thresholds and
        (marker['terminal_at'], key) < thresholds[kind] or
        'completed_order' in marker and kind in order_thresholds and
        (marker['completed_order'] < order_thresholds[kind][0]
         if marker.get('order_upper_bound') else
         (marker['completed_order'], key) < order_thresholds[kind]))


def candidate_safe(tx, kind, key, value, marker):
    """Pure canonical safety checks; tx only needs get, never write methods."""
    if not isinstance(value, dict) or not terminal_safe(kind, value):
        return False
    if (kind == 'actions' and not value.get('result_expired') and
            any(f not in value for f in ('id', 'state', 'provider', 'contract',
                                        'inputs', 'resource', 'generation', 'lease'))):
        return False
    if kind == 'leases':
        from .lease_retention import safe_released
        if not safe_released(tx, key, value):
            return False
        if marker.get('observation_digest') and marker['observation_digest'] != digest(value):
            return False
    if kind == '_submissions' and tx.get('_submission_active', key) is not None:
        return False
    return True


def _expired_summaries(tx, records, budget, limit=64):
    """Bounded keyset recovery of summaries written by older retention versions."""
    kinds=sorted(('_receipt','_submissions','actions','runs'))
    tx.db.execute("INSERT OR IGNORE INTO retention_candidate_cursor VALUES(2,'')")
    raw=tx.db.execute('SELECT marker FROM retention_candidate_cursor WHERE id=2').fetchone()[0]
    kind,after=json.loads(raw) if raw else (kinds[0],'')
    if kind not in kinds:raise ValueError('Invalid expired-summary cursor')
    rows=tx.db.execute('SELECT r.id,length(CAST(r.value AS BLOB)),s.ignored FROM records r LEFT JOIN retention_ref_sources s ON s.kind=r.kind AND s.id=r.id WHERE r.kind=? AND r.id>? ORDER BY r.id LIMIT ?', (kind,after,limit)).fetchall()
    result=[];used=0
    for key,size,ignored in rows:
        after=key
        if ignored!=1 or size>32768:continue
        if (kind,key) in records:value=records[(kind,key)]
        else:
            if used+size>budget:continue
            value=tx.get(kind,key);used+=size
        if not isinstance(value,dict) or value.get('result_expired') is not True:continue
        if not terminal_safe(kind,value):continue
        if kind=='_submissions' and tx.get('_submission_active',key) is not None:continue
        result.append(((kind,key),value))
    if len(rows)<limit:
        kind=kinds[(kinds.index(kind)+1)%len(kinds)];after=''
    tx.db.execute('UPDATE retention_candidate_cursor SET marker=? WHERE id=2',(json.dumps([kind,after]),))
    return result


def sweep(tx, now=None, seconds=86400, keep=1024, batch=128, index_ready=None):
    """Delete eligible completed records by elapsed time or rolling count, preserving roots.

    ``keep`` counts completed records per kind. Active/referenced records are
    exempt; this is not an absolute database size limit. Completed identity summaries are not an indefinite retention exception.
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
        if m['kind'] == '_submissions' and not any(field in m for field in ('terminal_at', 'completed_order', 'observed_before')):
            # Scalar projection avoids decoding a possibly large action result.
            row = tx.db.execute("SELECT value FROM record_lookup WHERE kind='actions' AND id=? AND field='state' AND NOT EXISTS (SELECT 1 FROM record_lookup_dirty WHERE kind='actions' AND id=record_lookup.id)",
                                (m['key'],)).fetchone()
            if row and json.loads(row[0]) in TERMINAL['actions'] and tx.get('_submission_active', m['key']) is None:
                # This proves completion was observed by now, not its original
                # date/order. Never insert a fabricated terminal count rank.
                m['observed_before'] = now
                tx.put('_retention', m['id'], m)
    # Count policy uses compact marker metadata only, never operational values.
    thresholds = {}
    order_thresholds = {}
    if keep:
        for kind in KINDS:
            row = tx.db.execute("SELECT terminal,key FROM retention_markers WHERE kind=? "
                                "AND terminal IS NOT NULL ORDER BY terminal DESC,key DESC LIMIT 1 OFFSET ?",
                                (kind, keep - 1)).fetchone()
            if row:
                thresholds[kind] = row
            order_table = 'retention_exact_order' if kind == '_receipt' else 'retention_original_order'
            row = tx.db.execute("SELECT position,key FROM " + order_table + " WHERE kind=? "
                                "ORDER BY position DESC,key DESC LIMIT 1 OFFSET ?", (kind, keep - 1)).fetchone()
            if row:
                order_thresholds[kind] = row
    candidates = {}
    for kind in KINDS:
        ready = sorted((m for m in markers if m['kind'] == kind and ('terminal_at' in m or 'completed_order' in m or 'observed_before' in m)),
                       key=lambda m: (m.get('terminal_at', m.get('completed_order', 0)), m['key']))
        for index, m in enumerate(ready):
            key = (kind, m['key'])
            if (key in records and window_expired(m, now, seconds, keep, thresholds, order_thresholds)
                    and candidate_safe(tx, kind, m['key'], records[key], m)):
                candidates[key] = m
    # Previously compacted rows have already crossed their rolling window.
    for key,value in _expired_summaries(tx,records,payload_budget-used):
        records[key]=value
        candidates[key]={'id':key[0]+':'+key[1],'kind':key[0],'key':key[1]}
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
    priority = {'_receipt': 0, '_submissions': 1, 'runs': 2, 'actions': 3, 'events': 4, 'artifacts': 5, 'recovery_compactions': 6, 'leases': 7}
    candidates = dict(sorted(eligible.items(), key=lambda item:
                           (priority[item[0][0]], item[1].get('terminal_at', item[1].get('completed_order', 0)), item[0]))[:batch])
    protected = protected_by_roots(candidates)
    changed = 0
    meta = tx.get('_meta', META, {})
    floor = meta.get('event_floor', 0)
    for key, marker in sorted(candidates.items(), key=lambda item: (item[1].get('terminal_at', item[1].get('completed_order', 0)), item[0])):
        if changed >= batch:
            break
        if key in protected:
            continue
        kind, ident = key
        value = records[key]
        if kind=='leases':
            from .lease_retention import safe_released
            if not safe_released(tx,ident,value):continue
            from .lease_retention import disposable_guard
            old_guard=disposable_guard(tx,ident,value)
            expected_shared='!shared:'+digest([value.get('resource'),ident])
            if value.get('guard')==expected_shared and tx.get('guards',expected_shared) is not None and old_guard is None:continue
            if old_guard:tx.drop('guards',old_guard)
            tx.drop('_reservation_activity',ident)
        tx.drop(kind, ident)
        if kind == '_submissions':
            lease=value.get('lease')
            if lease and tx.get('_submission_leases',lease)==ident:
                tx.drop('_submission_leases',lease)
        if kind == 'events':
            floor = max(floor, value['seq'])
        tx.drop('_retention', marker['id'])
        changed += 1
    if floor != meta.get('event_floor', 0):
        meta['event_floor'] = floor
        tx.put('_meta', META, meta)
    return changed
