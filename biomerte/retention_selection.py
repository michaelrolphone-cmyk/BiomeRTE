"""Read-only historical retention selection for bounded migration snapshots.

This is policy selection over a complete canonical snapshot, not a live sweep.
It never opens a Store, advances a cursor, rebuilds an index, or writes SQL.
The caller owns snapshot consistency, input bounds and approval of the returned
hash-bound omissions. Scheduling batch sizes do not change retention windows.
"""
import hashlib
import math

from . import retention, retention_history
from .lease_retention import safe_released
from .retention_index import CHUNK, MAX_REF, reference_hash
from .store import digest, encode


class _ReadView:
    """Read-only canonical accessor with in-memory enrollment evidence only."""
    def __init__(self, records):
        self.records = records
        self.db = records.db
        self.overlay = {}

    def get(self, kind, key, default=None):
        ref = (kind, key)
        if ref in self.overlay:
            return self.overlay[ref]
        return self.records.get(ref, default)


def _historical_markers(view):
    # Reuse the existing provenance checks, including exact completion events,
    # original receipt timestamps and bounded, explicit historical exceptions.
    cursor = {'phase': 'events', 'key': '', 'kind': ''}
    exceptions = []
    while cursor['phase'] != 'done':
        view.overlay[('_meta', retention_history.CURSOR)] = cursor
        page = retention_history.plan(view)
        for marker in page['markers']:
            view.overlay[('_retention', marker['id'])] = marker
        exceptions.extend(page['exceptions'])
        if page['next'] == cursor:
            raise ValueError('Historical eligibility cursor did not advance')
        cursor = page['next']
    # The migration is not a historical-enrollment transaction. Do not copy a
    # made-up completed cursor or overwrite the real source cursor.
    del view.overlay[('_meta', retention_history.CURSOR)]
    return exceptions


def _reference_strings(value):
    # Match the JSON-token reference projection, including string map keys.
    if isinstance(value, str):
        if len(value) <= MAX_REF:
            yield value
        if value.startswith('action:') and len(value[7:]) <= MAX_REF:
            yield value[7:]
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _reference_strings(key)
            yield from _reference_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _reference_strings(item)


class _CanonicalReferences:
    """Disposable in-memory projection, derived only from canonical records."""
    def __init__(self, records, candidates, sizes, dependency_limit, annotations=None):
        self.records = records
        self.annotations = annotations or {}
        self.aliases, self.oversized, self.sources = {}, set(), []
        for ref in candidates:
            value = records[ref]
            aliases = [ref[1]]
            if isinstance(value, dict) and isinstance(value.get('id'), str):
                aliases.append(value['id'])
            for alias in aliases:
                if len(alias) > MAX_REF:
                    self.oversized.add(ref)
                else:
                    self.aliases.setdefault(reference_hash(alias), set()).add(ref)
        canonical_hash, self.source_rows, edges = hashlib.sha256(), 0, 0
        for ref, value in records.items():
            canonical_hash.update(encode([ref[0], ref[1], value]).encode('utf-8') + b'\n')
            self.source_rows += 1
            kind, key = ref
            ignored = kind in ('_retention', '_meta')
            submission = None
            # The existing reference index exempts settled sources only when
            # they fit its metadata decode bound. Preserve that conservatism.
            small = sizes[ref][1] <= CHUNK
            if small and isinstance(value, dict):
                ignored |= (kind in ('actions', 'runs', '_receipt', '_submissions') and
                            bool(value.get('result_expired')))
                ignored |= kind == 'leases' and value.get('state') == 'released'
            if small and kind == '_submission_leases' and isinstance(value, str):
                submission = value
            if ignored:
                continue
            targets = set()
            body = dict(value) if ref in self.annotations else value
            if ref in self.annotations:
                body.pop(self.annotations[ref]['field'], None)
            for word in _reference_strings(body):
                targets.update(self.aliases.get(reference_hash(word), ()))
                if edges + len(targets) > dependency_limit:
                    raise ValueError('Retention dependency inventory exceeds safety bound')
            if targets:
                self.sources.append((ref, submission, targets))
                edges += len(targets)
        self.canonical_sha256 = canonical_hash.hexdigest()
        self.edges_sha256 = digest([[list(ref), submission, sorted(targets)]
                                    for ref, submission, targets in self.sources])

    def protected(self, pool):
        pool = set(pool)
        protected = self.oversized & pool
        for ref, submission, targets in self.sources:
            if ref in pool:
                continue
            if submission is not None and (
                    ('_submissions', submission) in pool or
                    self.records.get(('_submissions', submission), {}).get('result_expired')):
                continue
            protected.update(targets & pool)
        pending = list(protected)
        # Match the existing fixed-point propagation, not only direct roots.
        by_id = {}
        for ref in pool:
            value = self.records[ref]
            for alias in (ref[1], value.get('id') if isinstance(value, dict) else None):
                if isinstance(alias, str):
                    by_id.setdefault(alias, set()).add(ref)
        while pending:
            for word in retention.strings(self.records[pending.pop()]):
                for ref in by_id.get(word, ()):
                    if ref not in protected:
                        protected.add(ref)
                        pending.append(ref)
        return protected


def plan(records, now, seconds=86400, keep=1024, historical=True, dependency_limit=200000, repair_review=None):
    """Return exact omitted preimages and retained enrollment evidence.

    ``records`` is a bounded mapping of (kind, id) to JSON values with a ``db``
    exposing the same stable SQLite snapshot. Only SELECT statements are used.
    Dirty, missing or forged derived indexes cannot influence this selection.
    Historical enrollment is virtual; missing provenance is retained explicitly.
    """
    if (type(now) not in (int, float) or not math.isfinite(now) or now < 0 or
            type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0 or
            type(keep) is not int or keep < 0 or type(historical) is not bool or
            type(dependency_limit) is not int or dependency_limit < 1):
        raise ValueError('Invalid operational retention selection policy')
    view = _ReadView(records)
    sizes = {(kind, key): (byte_size, chars) for kind, key, byte_size, chars in records.db.execute(
        'SELECT kind,id,length(CAST(value AS BLOB)),length(value) FROM records')}
    exceptions = _historical_markers(view) if historical else []
    markers = {}
    for ref, value in records.items():
        if ref[0] == '_retention':
            markers[ref[1]] = value
    for ref, value in view.overlay.items():
        if ref[0] == '_retention':
            markers[ref[1]] = value
    for marker_id, marker in list(markers.items()):
        if (not isinstance(marker, dict) or marker.get('id') != marker_id or
                marker.get('kind') not in retention.KINDS or
                not isinstance(marker.get('key'), str) or
                marker_id != marker['kind'] + ':' + marker['key']):
            raise ValueError('Invalid canonical retention marker: ' + marker_id)
        if marker['kind'] == '_submissions' and not any(field in marker for field in ('terminal_at', 'completed_order', 'observed_before')):
            action = view.get('actions', marker['key'])
            if (isinstance(action, dict) and action.get('state') in retention.TERMINAL['actions'] and
                    view.get('_submission_active', marker['key']) is None):
                marker = dict(marker, observed_before=now)
                markers[marker_id] = marker
                view.overlay[('_retention', marker_id)] = marker

    thresholds, order_thresholds = {}, {}
    if keep:
        for kind in retention.KINDS:
            typed = [m for m in markers.values() if m['kind'] == kind and
                     (len(encode(m)) if ('_retention', m['id']) in view.overlay else
                      sizes[('_retention', m['id'])][1]) <= CHUNK]
            dated = sorted((m['terminal_at'], m['key']) for m in typed if m.get('terminal_at') is not None)
            ordered = sorted((m['completed_order'], m['key']) for m in typed
                             if isinstance(m.get('completed_order'), int) and
                             (kind != '_receipt' or not m.get('order_upper_bound')))
            if len(dated) >= keep:
                thresholds[kind] = dated[-keep]
            if len(ordered) >= keep:
                order_thresholds[kind] = ordered[-keep]

    candidates, reasons = {}, {}
    values = records  # Payloads stay streaming; retain only scalar selection metadata.
    for marker in markers.values():
        ref = (marker['kind'], marker['key'])
        value = records.get(ref)
        if (value is None or sizes[ref][0] > retention_history.MAX_BYTES or
                not any(field in marker for field in ('terminal_at', 'completed_order', 'observed_before'))):
            continue
        if (retention.window_expired(marker, now, seconds, keep, thresholds, order_thresholds) and
                retention.candidate_safe(view, ref[0], ref[1], value, marker)):
            candidates[ref], reasons[ref] = marker, 'retention_window'
    # Whole-row retirement includes old compacted summaries, not permanent
    # replay tombstones. The same small-payload and terminal guards apply.
    for ref, value in records.items():
        if (ref[0] in ('actions', 'runs', '_receipt', '_submissions') and
                isinstance(value, dict) and value.get('result_expired') is True and
                sizes[ref][0] <= CHUNK and
                retention.terminal_safe(ref[0], value) and
                (ref[0] != '_submissions' or view.get('_submission_active', ref[1]) is None)):
            candidates[ref] = {'id': ref[0] + ':' + ref[1], 'kind': ref[0], 'key': ref[1]}
            reasons[ref] = 'expired_summary'
    cursors = []
    for ref, value in records.items():
        if ref[0] == 'subscriptions' and value.get('process'):
            cursors.append(value['cursor'])
        elif ref[0] == 'assistant_wake_outbox':
            cursors.append(value.get('cursor', 0))
    for ref, marker in list(candidates.items()):
        if (ref[0] == 'artifacts' and (not marker.get('operational') or marker.get('durable')) or
                ref[0] == 'events' and cursors and values[ref]['seq'] > min(cursors)):
            del candidates[ref]

    from .retention_repairs import classify as classify_repairs
    repair_candidates, annotations, qualified_repairs = classify_repairs(records, now, seconds, keep, repair_review, thresholds, order_thresholds)
    for ref, reason in repair_candidates.items():
        candidates[ref] = {'id': ref[0] + ':' + ref[1], 'kind': ref[0], 'key': ref[1]}
        reasons[ref] = reason

    # Include disposable shared guards in reachability, but only as adjuncts to
    # an actually selected released lease. No reusable/current fence is removed.
    guards = {}
    for ref in candidates:
        if ref[0] != 'leases':
            continue
        value = values[ref]
        guard = '!shared:' + digest([value.get('resource'), ref[1]])
        if value.get('guard') == guard and records.get(('guards', guard)) is not None:
            guards[ref] = ('guards', guard)
    refs = _CanonicalReferences(records, set(candidates) | set(guards.values()), sizes, dependency_limit, annotations)
    selected = set(candidates)
    while True:
        pool = selected | {guard for lease, guard in guards.items() if lease in selected}
        protected = refs.protected(pool)
        blocked = protected & selected
        for lease, guard_ref in guards.items():
            if lease not in selected:
                continue
            value, guard = values[lease], values[guard_ref]
            if (not isinstance(guard, dict) or guard.get('holder') or
                    guard.get('generation') != value.get('generation') or guard_ref in protected or
                    not safe_released(view, lease[1], value)):
                blocked.add(lease)
        if not blocked:
            break
        selected -= blocked

    omitted = {ref: reasons[ref] for ref in selected}
    for ref in selected:
        kind, key = ref
        marker_ref = ('_retention', candidates[ref]['id'])
        if marker_ref in records:
            omitted[marker_ref] = 'retention_marker'
        if kind == '_submissions':
            lease = values[ref].get('lease')
            mapping = ('_submission_leases', lease)
            if lease and records.get(mapping) == key:
                omitted[mapping] = 'submission_lease_mapping'
        elif kind == 'leases':
            activity = ('_reservation_activity', key)
            if activity in records:
                omitted[activity] = 'released_lease_activity'
            if ref in guards:
                omitted[guards[ref]] = 'released_shared_guard'
    meta = records.get(('_meta', retention.META), {})
    floor = max([meta.get('event_floor', 0)] + [values[ref]['seq'] for ref in selected if ref[0] == 'events'])
    result = {
        'version': 1, 'policy': {'name': 'operational_retention_v1', 'now': now, 'seconds': seconds,
                                 'keep': keep, 'historical': historical,
                                 'terminal_thresholds': thresholds, 'order_thresholds': order_thresholds},
        'omitted': [dict(kind=ref[0], id=ref[1], sha256=digest(records[ref]), reason=omitted[ref])
                    for ref in sorted(omitted)],
        'repair_review': repair_review,
        'qualified_repairs': [dict(kind=ref[0], id=ref[1], sha256=digest(records[ref]))
                              for ref in sorted(qualified_repairs)],
        'record_updates': [dict(kind=ref[0], id=ref[1], before_sha256=digest(records[ref]),
                                remove_fields=[item['field']])
                           for ref, item in sorted(annotations.items())
                           if ref not in omitted],
        'event_floor': {'before': meta.get('event_floor', 0), 'after': floor},
        'marker_updates': [dict(id=ref[1], before_sha256=digest(records.get(ref)), value=value)
                           for ref, value in sorted(view.overlay.items())
                           if (value['kind'], value['key']) not in selected and records.get(ref) != value],
        'exceptions': sorted(exceptions),
        # These are observed legacy repair schemas, not a retention exemption.
        # Until a schema-specific completion proof exists, expose the exact
        # classification gap rather than silently declaring them permanent.
        'unclassified_repair': [dict(kind=ref[0], id=ref[1], sha256=digest(value),
                                    requires=['qualified_record_schema',
                                              'original_completion_and_quiescence_evidence',
                                              'current_dependency_and_authority_classification'])
                                for ref, value in records.items()
                                if ref[0] in ('_operator_purges', '_operator_purge_before', '_operator_purge_units', 'assistant_recovery_receipts') and ref not in qualified_repairs],
        'dependency_evidence': {'method': 'canonical_fixed_point_v1',
                                'canonical_records_sha256': refs.canonical_sha256,
                                'canonical_rows': refs.source_rows, 'edges_sha256': refs.edges_sha256,
                                'dependency_limit': dependency_limit,
                                'protected': [list(ref) for ref in sorted(set(candidates) - selected)]},
    }
    result['sha256'] = digest(result)
    return result
