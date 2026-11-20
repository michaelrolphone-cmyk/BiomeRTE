"""Narrow, read-only classification of verified completed repair diagnostics.

This is not an execution repair and never settles actions or clears fences.
The installed-reader review is an explicit qualification input; an unfamiliar
schema remains a root. Backup pointers are metadata, never instructions to read
or delete the referenced files. Current operational records are preserved.
"""
import calendar
import datetime
import re
from .retention import window_expired, strings, terminal_safe
from .store import digest

PURGES = '_operator_purges'
BEFORE = '_operator_purge_before'
UNITS = '_operator_purge_units'
RECOVERY = 'assistant_recovery_receipts'
KINDS = frozenset((PURGES, BEFORE, UNITS, RECOVERY))


def timestamp(value):
    if not isinstance(value, str):
        return None
    for fmt in ('%Y-%m-%dT%H:%M:%S.%f+00:00', '%Y-%m-%dT%H:%M:%S+00:00',
                '%Y-%m-%dT%H:%M:%S.%fZ', '%Y-%m-%dT%H:%M:%SZ'):
        try:
            parsed = datetime.datetime.strptime(value, fmt)
            return calendar.timegm(parsed.timetuple()) + parsed.microsecond / 1000000.0
        except ValueError:
            pass
    return None


def counts(value):
    return isinstance(value, dict) and all(isinstance(k, str) and type(v) is int and v >= 0
                                          for k, v in value.items())


def classify(records, now, seconds, keep, review, thresholds=None, order_thresholds=None):
    """Return candidates and exact ignorable annotations, not a blanket exemption."""
    if review is None:
        return {}, {}, set()
    if (not isinstance(review, dict) or review.get('schema') != 'completed_repair_v1' or
            any(not isinstance(review.get(k), str) or not review[k].strip()
                for k in ('installed_reader_evidence', 'canonical_schema_evidence'))):
        raise ValueError('Completed repair schema/reader qualification is missing')
    roots, all_roots, candidates, annotations, recovered, qualified = {}, {}, {}, {}, {}, set()
    root_fields = {'completed_at', 'counts', 'id', 'journal_mode', 'native_mac_quiescent',
                   'realm', 'remote_native_quiescence'}
    for ref, value in records.items():
        if ref[0] != PURGES or not isinstance(value, dict) or set(value) != root_fields:
            continue
        stamp = timestamp(value['completed_at'])
        if (value['id'] != ref[1] or value['realm'] not in ('site', 'local') or
                value['journal_mode'] != 'one_primary_record_per_transaction' or
                type(value['native_mac_quiescent']) is not bool or
                not isinstance(value['remote_native_quiescence'], str) or
                not counts(value['counts']) or stamp is None or stamp > now):
            continue
        marker = {'kind': PURGES, 'key': ref[1], 'terminal_at': stamp}
        all_roots[ref[1]] = value
        qualified.add(ref)
        if window_expired(marker, now, seconds, keep, {}, {}):
            roots[ref[1]] = value
            candidates[ref] = 'completed_repair_diagnostic'
    for ref, value in records.items():
        kind, key = ref
        if not isinstance(value, dict):
            continue
        root = all_roots.get(value.get('purge_id')) if isinstance(value.get('purge_id'), str) else None
        if root is not None and kind in (BEFORE, UNITS):
            common = {'purge_id', 'realm', 'kind', 'key'}
            required = common | ({'path', 'sha256', 'bytes'} if kind == BEFORE else {'counts'})
            valid = (set(value) == required and value['realm'] == root['realm'] and
                     isinstance(value['kind'], str) and isinstance(value['key'], str) and
                     re.match(r'^[a-f0-9]{64}$', key))
            if kind == BEFORE:
                valid = valid and (isinstance(value['path'], str) and isinstance(value['sha256'], str) and
                                   re.match(r'^[a-f0-9]{64}$', value['sha256']) and
                                   type(value['bytes']) is int and value['bytes'] >= 0)
            else:
                valid = valid and counts(value['counts'])
            if valid:
                qualified.add(ref)
                if value['purge_id'] in roots:
                    candidates[ref] = 'completed_repair_backup_pointer' if kind == BEFORE else 'completed_repair_unit'
        # Actual canonical fields and source-reader review establish these as
        # diagnostic annotations. No generic nested business references vanish.
        root_id = value.get('operator_purge')
        if kind in ('guards', 'assistant_responsibilities') and isinstance(root_id, str) and root_id in roots:
            if kind == 'guards' and not ('holder' in value and type(value.get('generation')) is int):
                continue
            annotations[ref] = {'field': 'operator_purge', 'root': (PURGES, root_id)}
        if kind != RECOVERY or value.get('id') != key:
            continue
        shape = set(value)
        if shape == {'id', 'episode_id', 'prior_run', 'reason', 'run_id'}:
            if not all(isinstance(v, str) for v in value.values()):
                continue
            wanted = [value['prior_run'], value['run_id']]
        elif shape == {'id', 'runs'} and isinstance(value['runs'], list):
            wanted = []
            valid = True
            for item in value['runs']:
                if isinstance(item, str):
                    wanted.append(item)
                elif isinstance(item, dict) and set(item) == {'id', 'before', 'after', 'steps', 'saved_state_sha256'}:
                    # Native schema evidence binds this diagnostic to the exact
                    # completed run state, not arbitrary nested retry authority.
                    current = records.get(('runs', item.get('id'))) if isinstance(item.get('id'), str) else None
                    if (not isinstance(current, dict) or
                            item['before'] not in ('completed', 'failed', 'cancelled') or
                            item['after'] not in ('completed', 'failed', 'cancelled') or
                            current.get('state') != item['after'] or
                            type(item['steps']) is not int or item['steps'] < 0 or
                            type(current.get('steps')) is not int or current['steps'] != item['steps'] or
                            not isinstance(item['saved_state_sha256'], str) or
                            digest(current) != item['saved_state_sha256']):
                        valid = False
                    wanted.append(item['id'])
                else:
                    valid = False
            if not valid:
                continue
        else:
            continue
        if not wanted or any(not isinstance(records.get(('runs', run)), dict) or
                             not terminal_safe('runs', records[('runs', run)]) for run in wanted):
            continue
        # A repair receipt cannot make an unresolved nested effect disappear.
        blocked = False
        for word in strings(value):
            for target in ('runs', 'actions'):
                item = records.get((target, word))
                if isinstance(item, dict) and not terminal_safe(target, item):
                    blocked = True
        episode = records.get(('assistant_episodes', value.get('episode_id')))
        if episode is not None:
            blocked = True  # Existing episode completion schema is not qualified here.
        if not blocked:
            recovered[key] = 0
    # Exact server-authored resume-event payload equality proves provenance.
    # The current rolling EVENT window supplies order, never a guessed date.
    for ref, event in records.items():
        if ref[0] != 'events' or not isinstance(event, dict):
            continue
        payload = event.get('payload')
        if not isinstance(payload, dict) or payload.get('id') not in recovered:
            continue
        seq = event.get('seq')
        if type(seq) is not int or seq < 1 or ref[1] != '%020d' % seq:
            continue
        key = payload['id']
        if event.get('topic') == 'assistant.recovery.resumed' and digest(payload) == digest(records[(RECOVERY, key)]):
            recovered[key] = max(recovered[key], seq)
    for key, order in recovered.items():
        if not order:
            continue
        marker = {'kind': 'events', 'key': '%020d' % order, 'completed_order': order}
        qualified.add((RECOVERY, key))
        if window_expired(marker, now, seconds, keep, thresholds or {}, order_thresholds or {}):
            candidates[(RECOVERY, key)] = 'completed_repair_event_window'
    return candidates, annotations, qualified
