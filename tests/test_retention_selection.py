"""Pure migration selection agrees with real sweeps on isolated fixtures."""
import sqlite3
import tempfile
import unittest

from biomerte import retention, retention_history, retention_index, retention_selection
from biomerte.store import Store, digest


class Snapshot:
    def __init__(self, db):
        self.db = db

    def __iter__(self):
        return iter(self.db.execute('SELECT kind,id FROM records ORDER BY kind,id'))

    def __contains__(self, ref):
        return self.db.execute('SELECT 1 FROM records WHERE kind=? AND id=?', ref).fetchone() is not None

    def get(self, ref, default=None):
        import json
        row = self.db.execute('SELECT value FROM records WHERE kind=? AND id=?', ref).fetchone()
        return json.loads(row[0]) if row else default

    def __getitem__(self, ref):
        if ref not in self:
            raise KeyError(ref)
        return self.get(ref)

    def items(self):
        for ref in self:
            yield ref, self[ref]


class HistoricalSelectionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(directory.name, 'fixture')

    def put(self, kind, key, value, at=None, **marker):
        def write(tx):
            if at is None:
                tx.put(kind, key, value)
            else:
                with retention.tracking(tx, at):
                    tx.put(kind, key, value)
            if marker:
                ident = kind + ':' + key
                old = tx.get('_retention', ident, {'id': ident, 'kind': kind, 'key': key})
                tx.put('_retention', ident, dict(old, **marker))
        self.store.execute(write)

    def action(self, key, state='succeeded', **extra):
        value = dict(id=key, state=state, provider='provider', lease='lease',
                     contract='contract', resource='resource', generation=1,
                     inputs={}, result={})
        value.update(extra)
        return value

    def contents(self):
        return self.store.execute(lambda tx: dict(Snapshot(tx.db).items()), read_only=True)

    def select(self, **policy):
        def read(tx):
            before = tx.db.total_changes
            denied = []
            def authorize(op, first, second, database, trigger):
                if op not in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION):
                    denied.append((op, first))
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            tx.db.set_authorizer(authorize)
            try:
                result = retention_selection.plan(Snapshot(tx.db), **policy)
            finally:
                tx.db.set_authorizer(None)
            self.assertEqual([], denied)
            self.assertEqual(before, tx.db.total_changes)
            self.assertEqual({}, tx.changes)
            return result
        return self.store.execute(read, read_only=True)

    def equivalent(self, now=100, seconds=10, keep=1024, historical=False):
        before = self.contents()
        result = self.select(now=now, seconds=seconds, keep=keep, historical=historical)
        self.assertEqual(before, self.contents())
        if historical:
            # Only this isolated fixture is enrolled/swept. The selector above
            # was authorizer-enforced read-only, including disposable tables.
            self.store.execute(lambda tx: tx.put('_meta', retention_history.CURSOR,
                                                {'phase': 'events', 'key': '', 'kind': ''}))
            for unused in range(1000):
                page = self.store.execute(lambda tx: retention_history.plan(tx), read_only=True)
                self.store.execute(lambda tx: retention_history.apply(tx, page))
                if page['next']['phase'] == 'done':
                    break
            else:
                self.fail('Fixture enrollment did not complete')
        for unused in range(80):
            self.store.execute(lambda tx: retention.sweep(tx, now=now, seconds=seconds, keep=keep))
        after = self.contents()
        omitted = {(item['kind'], item['id']) for item in result['omitted']}
        self.assertEqual(set(before) - set(after), omitted)
        for item in result['omitted']:
            self.assertEqual(digest(before[(item['kind'], item['id'])]), item['sha256'])
        meta = after.get(('_meta', retention.META), {})
        self.assertEqual(meta.get('event_floor', 0), result['event_floor']['after'])
        return omitted, result

    def test_time_count_windows_and_summary_retirement(self):
        for key, at in [('time-old', 1), ('boundary', 90), ('count-old', 99), ('new-1', 99), ('new-2', 99)]:
            self.put('actions', key, self.action(key), at=at)
        self.put('actions', 'summary', dict(retention.compact_action(self.action('summary')),
                                          result_expired=True, expired_at=1))
        omitted, result = self.equivalent(keep=2)
        self.assertIn(('actions', 'summary'), omitted)
        self.assertIn(('actions', 'boundary'), omitted)
        self.assertIn(('actions', 'count-old'), omitted)
        self.assertNotIn(('actions', 'new-1'), omitted)

    def test_active_nonquiescent_submission_and_transitive_roots(self):
        self.put('artifacts', 'blob', {'id': 'blob', 'data': 'output'}, at=1, operational=True)
        self.put('actions', 'old', self.action('old', result={'artifact': 'blob'}), at=1)
        self.put('runs', 'active', {'id': 'active', 'state': 'running', 'action': 'old'}, at=1)
        self.put('actions', 'uncertain', self.action('uncertain', state='uncertain'), at=1)
        self.put('actions', 'nonquiet', self.action('nonquiet', result={'evidence': {'quiescent': False}}), at=1)
        self.put('actions', 'reopened', self.action('reopened', state='claimed'), at=1, terminal_at=1)
        self.put('_submissions', 'pending', {'id': 'pending', 'lease': 'pending-lease'}, at=1, terminal_at=1)
        self.put('_submission_active', 'pending', {'inflight': True})
        self.put('actions', 'free', self.action('free'), at=1)
        omitted, result = self.equivalent()
        self.assertEqual({('actions', 'free'), ('_retention', 'actions:free')}, omitted)
        self.assertIn(['actions', 'old'], result['dependency_evidence']['protected'])

    def test_unread_event_uuid_and_map_key_reference_protection(self):
        for seq in range(1, 5):
            self.put('events', '%020d' % seq, {'id': 'uuid-' + str(seq), 'seq': seq,
                                            'topic': 'trace', 'payload': {}}, at=1)
        self.put('subscriptions', 'process', {'process': 'definition', 'cursor': 2})
        self.put('assistant_wake_outbox', 'wake', {'cursor': 3})
        self.put('revisions', 'reference', {'uuid-1': True})
        omitted, result = self.equivalent()
        self.assertEqual({('events', '%020d' % 2), ('_retention', 'events:%020d' % 2)}, omitted)
        self.assertEqual({'before': 0, 'after': 2}, result['event_floor'])

    def test_durable_and_unmarked_artifacts_do_not_expire(self):
        self.put('artifacts', 'temporary', {'id': 'temporary', 'data': 'T'}, at=1, operational=True)
        self.put('artifacts', 'shared', {'id': 'shared', 'data': 'S'}, at=1, operational=True, durable=True)
        self.put('artifacts', 'unknown', {'id': 'unknown', 'data': 'U'}, at=1)
        self.put('artifacts', 'legacy', {'id': 'legacy', 'data': 'L'})
        omitted, result = self.equivalent(historical=True)
        self.assertEqual({('artifacts', 'temporary'), ('_retention', 'artifacts:temporary')}, omitted)

    def test_historical_evidence_preserves_exact_and_upper_bound_order(self):
        for key, position, upper in [('cohort-a', 10, True), ('cohort-z', 10, True),
                                     ('exact-a', 11, False), ('exact-z', 12, False)]:
            self.put('_receipt', key, {'signature': key, 'result': {}},
                     completed_order=position, order_upper_bound=upper)
        self.put('_receipt', 'unproven', {'signature': 'unproven', 'result': {}})
        self.put('_receipt', 'timestamp', {'format': 2, 'signature': 'timestamp', 'created_at': 1, 'result': {}})
        self.put('recovery_compactions', 'repair-old', {'request_id': 'repair-old', 'previous_head': 1, 'removed': []})
        self.put('recovery_compactions', 'repair-new', {'request_id': 'repair-new', 'previous_head': 100, 'removed': []})
        self.put('actions', 'completed', self.action('completed'))
        self.put('events', '%020d' % 1, {'id': 'event-1', 'seq': 1, 'topic': 'operation.succeeded', 'payload': {'id': 'completed'}})
        self.put('events', '%020d' % 2, {'id': 'event-2', 'seq': 2, 'topic': 'trace', 'payload': {}})
        omitted, result = self.equivalent(keep=1, historical=True)
        for ref in [('_receipt', 'cohort-a'), ('_receipt', 'cohort-z'), ('_receipt', 'exact-a'),
                    ('_receipt', 'timestamp'), ('recovery_compactions', 'repair-old'), ('events', '%020d' % 1)]:
            self.assertIn(ref, omitted)
        for ref in [('_receipt', 'unproven'), ('_receipt', 'exact-z'), ('recovery_compactions', 'repair-new')]:
            self.assertNotIn(ref, omitted)
        self.assertTrue(any(m['id'] == 'actions:completed' for m in result['marker_updates']))

    def test_dirty_and_missing_derived_indexes_use_canonical_active_reference(self):
        self.put('actions', 'kept', self.action('kept'), at=1)
        self.put('actions', 'deleted', self.action('deleted'), at=1)
        self.store.execute(lambda tx: retention_index.advance(tx.db, max_rows=1000))
        self.put('runs', 'new-active', {'id': 'new-active', 'state': 'running', 'result': 'kept'})
        before = self.select(now=100, seconds=10, historical=False)
        def corrupt(tx):
            tx.db.execute('DELETE FROM retention_refs')
            tx.db.execute('DELETE FROM retention_markers')
            tx.db.execute('DELETE FROM retention_original_order')
            tx.db.execute("UPDATE retention_ref_cursor SET kind='',id='',complete=0")
        self.store.execute(corrupt)
        self.assertEqual(before, self.select(now=100, seconds=10, historical=False))
        omitted, result = self.equivalent()
        self.assertIn(('actions', 'deleted'), omitted)
        self.assertNotIn(('actions', 'kept'), omitted)
        self.store.execute(lambda tx: tx.db.execute('DROP TABLE retention_ref_sources'))
        # The read-only plan does not even require disposable schema to exist.
        self.select(now=100, seconds=10, historical=False)

    def test_released_leases_current_guards_and_submission_mapping(self):
        for key in ('released', 'held', 'changed', 'shared', 'referenced'):
            guard = '!shared:' + digest(['resource', key]) if key in ('shared', 'referenced') else 'guard-' + key
            value = {'id': key, 'state': 'released', 'resource': 'resource', 'generation': 1, 'guard': guard}
            self.put('leases', key, value, at=1)
            self.put('guards', guard, {'id': guard, 'holder': key if key == 'held' else None, 'generation': 1})
            self.put('_reservation_activity', key, {'generation': 2 if key == 'changed' else 1})
            if key == 'referenced':
                self.put('revisions', 'guard-owner', {'guard': guard})
        self.put('_submissions', 'done', {'id': 'done', 'lease': 'released'}, terminal_at=1)
        self.put('_submission_leases', 'released', 'done')
        self.put('actions', 'done', self.action('done', lease='released'), at=1)
        omitted, result = self.equivalent()
        self.assertIn(('leases', 'released'), omitted)
        self.assertIn(('leases', 'shared'), omitted)
        self.assertIn(('_submission_leases', 'released'), omitted)
        self.assertNotIn(('leases', 'held'), omitted)
        self.assertNotIn(('leases', 'changed'), omitted)
        self.assertNotIn(('leases', 'referenced'), omitted)

    def test_old_recovery_history_is_not_blanket_protected_or_tombstoned(self):
        for key in ('old', 'recent', 'active-owned'):
            self.put('recovery_compactions', key,
                     {'request_id': key, 'previous_head': 4, 'removed': []},
                     at=99 if key == 'recent' else 1)
        self.put('runs', 'current', {'id': 'current', 'state': 'running', 'repair': 'active-owned'})
        self.put('unknown_repair', 'unknown', {'ref': 'do-not-infer-completion'})
        omitted, result = self.equivalent(historical=True)
        self.assertIn(('recovery_compactions', 'old'), omitted)
        self.assertNotIn(('recovery_compactions', 'active-owned'), omitted)
        self.assertNotIn(('recovery_compactions', 'recent'), omitted)
        self.assertNotIn(('unknown_repair', 'unknown'), omitted)

    def test_retained_history_marker_does_not_reset_original_timestamp(self):
        self.put('_receipt', 'recent', {'format': 2, 'created_at': 99, 'signature': 's', 'result': {}})
        result = self.select(now=100, seconds=10)
        self.assertEqual([], result['omitted'])
        self.assertEqual(99, result['marker_updates'][0]['value']['terminal_at'])
        self.assertEqual(digest(None), result['marker_updates'][0]['before_sha256'])
        self.assertEqual(result, self.select(now=100, seconds=10))

    def test_observation_age_boundary_and_changed_lease_digest(self):
        for key in ('exact-boundary', 'too-recent', 'changed'):
            value = {'id': key, 'state': 'released', 'generation': 1, 'resource': 'r'}
            self.put('leases', key, value, observed_before=90 if key != 'too-recent' else 91,
                     observation_digest=digest(dict(value, generation=0)) if key == 'changed' else digest(value))
        omitted, result = self.equivalent()
        self.assertIn(('leases', 'exact-boundary'), omitted)
        self.assertNotIn(('leases', 'too-recent'), omitted)
        self.assertNotIn(('leases', 'changed'), omitted)

    def test_upper_bound_cohort_cannot_rank_its_members(self):
        for key in ('cohort-a', 'cohort-z'):
            self.put('_receipt', key, {'signature': key, 'result': {}},
                     completed_order=10, order_upper_bound=True)
        omitted, result = self.equivalent(keep=1)
        self.assertEqual(set(), omitted)
        self.assertNotIn('_receipt', result['policy']['order_thresholds'])

    def test_raw_payload_size_and_oversized_root_conservatism(self):
        from biomerte.store import encode
        self.put('actions', 'unbounded', self.action('unbounded', result={'data': 'x' * 1048576}), at=1)
        summary = dict(retention.compact_action(self.action('large-summary')), result_expired=True)
        self.put('actions', 'large-summary', summary)
        # Raw SQLite size, not re-encoded compact JSON, controls old-summary
        # eligibility and whether settled sources are exempt reference roots.
        self.store.execute(lambda tx: tx.db.execute(
            'UPDATE records SET value=? WHERE kind=? AND id=?',
            (encode(summary) + ' ' * 32768, 'actions', 'large-summary')))
        self.put('actions', 'referenced', self.action('referenced'), at=1)
        self.put('leases', 'large-release', {'id': 'large-release', 'state': 'released',
                                          'ref': 'referenced', 'data': 'z' * 32768})
        omitted, result = self.equivalent()
        self.assertEqual(set(), omitted)

    def test_unknown_repair_classification_gap_is_exact_and_hash_bound(self):
        value = {'id': 'repair', 'completed_at': '2026-11-06', 'native_mac_quiescent': False}
        self.put('_operator_purges', 'repair', value)
        result = self.select(now=100)
        self.assertEqual([], result['omitted'])
        gap = result['unclassified_repair'][0]
        self.assertEqual(('_operator_purges', 'repair', digest(value)),
                         (gap['kind'], gap['id'], gap['sha256']))
        self.assertIn('current_dependency_and_authority_classification', gap['requires'])

    def test_dependency_bound_rejects_whole_plan(self):
        for key in ('a', 'b'):
            self.put('actions', key, self.action(key), at=1)
        self.put('work', 'owner', {'refs': ['a', 'b']})
        before = self.contents()
        with self.assertRaisesRegex(ValueError, 'dependency inventory'):
            self.select(now=100, seconds=10, dependency_limit=1)
        self.assertEqual(before, self.contents())

    def test_undated_submission_observation_never_displaces_real_count_date(self):
        for key in ('dated', 'undated'):
            self.put('actions', key, self.action(key))
            self.put('_submissions', key, {'id': key, 'lease': 'lease-' + key})
        self.put('_retention', '_submissions:dated', {'id': '_submissions:dated',
                 'kind': '_submissions', 'key': 'dated', 'terminal_at': 99})
        self.put('_retention', '_submissions:undated', {'id': '_submissions:undated',
                 'kind': '_submissions', 'key': 'undated', 'born': 1})
        omitted, result = self.equivalent(now=100, seconds=1000, keep=1)
        self.assertNotIn(('_submissions', 'dated'), omitted)
        self.assertEqual(result['policy']['terminal_thresholds']['_submissions'], (99, 'dated'))
        observation = next(x['value'] for x in result['marker_updates'] if x['id'] == '_submissions:undated')
        self.assertEqual(observation['observed_before'], 100)
        self.assertNotIn('terminal_at', observation)
        self.assertNotIn('completed_order', observation)
        later = self.select(now=110, seconds=1000, keep=1, historical=False)
        self.assertFalse(any(x['id'] == '_submissions:undated' for x in later['marker_updates']))

    def test_stored_observation_expires_only_by_its_real_time_bound(self):
        self.put('actions', 'done', dict(retention.compact_action(self.action('done')), result_expired=True))
        self.put('_submissions', 'done', {'id': 'done', 'lease': 'released'}, observed_before=90)
        self.assertFalse(any(x['kind'] == '_submissions' for x in self.select(now=99, seconds=10, keep=1, historical=False)['omitted']))
        omitted, result = self.equivalent(now=100, seconds=10, keep=1)
        self.assertIn(('_submissions', 'done'), omitted)


if __name__ == '__main__':
    unittest.main()
