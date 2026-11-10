import tempfile
import unittest
from biomerte.store import Store, Fault, encode, digest
from biomerte import retention, api, providers, process, knowledge


class OperationalRetentionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(self.directory.name, 'test')

    def write(self, kind, key, value, now=1):
        def run(tx):
            with retention.tracking(tx, now):
                tx.put(kind, key, value)
                if kind == 'artifacts' and key == 'blob':
                    marker = tx.get('_retention', 'artifacts:' + key)
                    marker['operational'] = True
                    tx.put('_retention', marker['id'], marker)
        self.store.execute(run)

    def sweep(self, **kwargs):
        return self.store.execute(lambda tx: retention.sweep(tx, now=100, seconds=10, **kwargs))

    def get(self, kind, key):
        return self.store.execute(lambda tx: tx.get(kind, key), read_only=True)

    def action(self, key='a', state='succeeded', **extra):
        return dict(id=key, state=state, provider='p', lease='l', contract='c',
                    resource='r', generation=1, inputs={'large': 'x' * 10000},
                    result={'large': 'y' * 10000}, **extra)

    def test_candidate_payload_budget_and_cursor_progress(self):
        for n in range(7):
            action = self.action(str(n))
            action.update(inputs={}, result={'payload': 'x' * 300000})
            self.write('actions', str(n), action)
        from biomerte.retention_index import advance
        for unused in range(100):
            if self.store.execute(lambda tx: advance(tx.db)):
                break
        seen = []
        def bounded(tx):
            original = tx.get
            def get(kind, key, default=None):
                value = original(kind, key, default)
                if kind == 'actions' and value is not None:
                    seen.append(len(encode(value)))
                return value
            tx.get = get
            return retention.sweep(tx, now=100, seconds=10)
        self.assertEqual(self.store.execute(bounded), 3)
        self.assertLessEqual(sum(seen), 1048576)
        for unused in range(100):
            self.sweep()
        self.assertTrue(all(self.get('actions', str(n)) is None for n in range(7)))

    def test_pinned_marker_page_does_not_starve_later_candidates(self):
        for n in range(140):
            action = self.action('%03d' % n)
            action.update(inputs={}, result={})
            self.write('actions', '%03d' % n, action)
        self.store.execute(lambda tx: tx.put('durable', 'root', {'refs': ['%03d' % n for n in range(139)]}))
        for unused in range(30):
            self.sweep(batch=1)
        self.assertIsNone(self.get('actions', '139'))
        self.assertFalse(self.get('actions', '000').get('result_expired', False))

    def test_sweep_has_no_schema_sql_and_rolls_back_state_with_markers(self):
        import sqlite3
        from biomerte.retention_index import advance, status
        self.write('actions', 'a', self.action())
        self.store.execute(lambda tx: advance(tx.db))
        before_action = self.get('actions', 'a')
        before_marker = self.get('_retention', 'actions:a')
        before_index = self.store.execute(lambda tx: status(tx.db), read_only=True)
        before_cursor = self.store.execute(lambda tx: tx.db.execute(
            'SELECT marker FROM retention_candidate_cursor WHERE id=1').fetchone(), read_only=True)
        schema_ops = {getattr(sqlite3, name) for name in (
            'SQLITE_CREATE_INDEX', 'SQLITE_CREATE_TABLE', 'SQLITE_CREATE_TEMP_INDEX',
            'SQLITE_CREATE_TEMP_TABLE', 'SQLITE_CREATE_TEMP_TRIGGER', 'SQLITE_CREATE_TEMP_VIEW',
            'SQLITE_CREATE_TRIGGER', 'SQLITE_CREATE_VIEW', 'SQLITE_DROP_INDEX', 'SQLITE_DROP_TABLE',
            'SQLITE_DROP_TEMP_INDEX', 'SQLITE_DROP_TEMP_TABLE', 'SQLITE_DROP_TEMP_TRIGGER',
            'SQLITE_DROP_TEMP_VIEW', 'SQLITE_DROP_TRIGGER', 'SQLITE_DROP_VIEW', 'SQLITE_ALTER_TABLE')}
        attempted_schema = []
        def authorize(op, arg1, arg2, database, origin):
            if op in schema_ops:
                attempted_schema.append((op, arg1))
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        class RollbackProbe(Exception):
            pass
        def abort(tx):
            tx.db.set_authorizer(authorize)
            self.assertEqual(retention.sweep(tx, now=100, seconds=10), 1)
            self.assertIsNone(tx.get('actions', 'a'))
            self.assertIsNone(tx.get('_retention', 'actions:a'))
            raise RollbackProbe()
        with self.assertRaises(RollbackProbe):
            self.store.execute(abort)
        self.assertEqual(attempted_schema, [])
        self.assertEqual(self.get('actions', 'a'), before_action)
        self.assertEqual(self.get('_retention', 'actions:a'), before_marker)
        self.assertEqual(self.store.execute(lambda tx: status(tx.db), read_only=True), before_index)
        self.assertEqual(self.store.execute(lambda tx: tx.db.execute(
            'SELECT marker FROM retention_candidate_cursor WHERE id=1').fetchone(), read_only=True), before_cursor)

    def test_legacy_untouched_even_when_updated(self):
        old = self.action()
        self.store.execute(lambda tx: tx.put('actions', 'a', old))
        self.write('actions', 'a', old)
        self.assertEqual(self.sweep(), 0)
        self.assertEqual(self.get('actions', 'a'), old)

    def test_deletes_expired_record_and_identity(self):
        self.write('actions', 'a', self.action())
        self.assertEqual(self.sweep(), 1)
        self.assertIsNone(self.get('actions', 'a'))
        self.assertIsNone(self.get('_retention', 'actions:a'))
        self.assertEqual(self.sweep(), 0)

    def test_active_uncertain_and_reopened_protected(self):
        self.write('actions', 'a', self.action(state='uncertain'))
        self.write('actions', 'b', self.action('b'))
        self.write('actions', 'b', self.action('b', 'claimed'), now=2)
        self.assertEqual(self.sweep(), 0)

    def test_unknown_and_legacy_reference_roots(self):
        self.write('actions', 'a', self.action())
        self.store.execute(lambda tx: tx.put('unknown_recovery', 'r', {'nested': ['a']}))
        self.assertEqual(self.sweep(), 0)

    def test_active_run_transitive_protection(self):
        self.write('artifacts', 'blob', {'id': 'blob', 'data': 'DATA'})
        self.write('actions', 'a', self.action(artifact='blob'))
        self.write('runs', 'r', {'id': 'r', 'state': 'running', 'action': 'a'})
        self.assertEqual(self.sweep(), 0)
        self.assertIsNotNone(self.get('artifacts', 'blob'))

    def test_operational_artifacts_deleted_user_artifacts_kept(self):
        self.write('artifacts', 'blob', {'id': 'blob', 'data': 'DATA'})
        self.write('artifacts', 'user', {'id': 'user', 'data': 'USER'})
        self.write('actions', 'a', self.action(artifact='blob'))
        self.sweep()
        self.assertIsNone(self.get('artifacts', 'blob'))
        self.assertIsNotNone(self.get('artifacts', 'user'))
        with self.assertRaises(Fault) as error:
            self.store.execute(lambda tx: knowledge.artifact_get(tx, None, 'blob'), read_only=True)
        self.assertEqual(error.exception.status, 410)

    def test_reference_in_knowledge_pins_artifact(self):
        self.write('artifacts', 'blob', {'id': 'blob', 'data': 'DATA'})
        self.write('actions', 'a', self.action(artifact='blob'))
        self.store.execute(lambda tx: tx.put('revisions', 'rev', {'body': {'sha256': 'blob'}}))
        self.sweep()
        self.assertIsNotNone(self.get('artifacts', 'blob'))

    def test_event_gap_explicit_and_legacy_preserved(self):
        self.store.execute(lambda tx: tx.emit('old', {}))
        def add(tx):
            with retention.tracking(tx, 1):
                tx.emit('new', {})
        self.store.execute(add)
        self.sweep()
        self.assertIsNotNone(self.get('events', '%020d' % 1))
        self.assertIsNone(self.get('events', '%020d' % 2))
        for call in (lambda tx: api.event_page(tx, None, 'new', 0),
                     lambda tx: api.events_after(tx, 0, '*'),
                     lambda tx: api.event_get(tx, None, 2)):
            with self.assertRaises(Fault) as error:
                self.store.execute(call, read_only=True)
            self.assertEqual(error.exception.status, 410)
        self.store.execute(lambda tx: api.event_get(tx, None, 1), read_only=True)

    def test_unread_process_and_wake_events_protected(self):
        def add(tx):
            with retention.tracking(tx, 1):
                tx.emit('trigger', {})
            tx.put('subscriptions', 'sub', {'process': 'p', 'cursor': 0})
        self.store.execute(add)
        self.assertEqual(self.sweep(), 0)

    def test_batch_does_not_delete_child_of_unselected_owner(self):
        self.write('artifacts', 'blob', {'id': 'blob', 'data': 'DATA'})
        self.write('actions', 'a', self.action(artifact='blob'), now=2)
        self.sweep(batch=1)
        self.assertIsNotNone(self.get('artifacts', 'blob'))

    def test_count_bound_and_idle_noop(self):
        for n in range(4):
            self.write('actions', str(n), self.action(str(n)), now=99)
        self.assertEqual(self.sweep(keep=2), 2)
        self.assertEqual(self.sweep(keep=2), 0)
        def run(tx):
            retention.sweep(tx, now=100, seconds=10, keep=2)
            self.assertEqual(tx.changes, {})
        self.store.execute(run)

    def test_deleted_action_is_absent(self):
        self.write('actions', 'a', self.action())
        self.sweep()
        with self.assertRaises(Fault) as error:
            self.store.execute(lambda tx: providers.status(tx, api.Context(None), 'a'), read_only=True)
        self.assertEqual(error.exception.status, 404)

    def test_only_versioned_future_receipts_expire(self):
        self.write('_receipt', 'old', {'signature': 's', 'result': {'operation': 'a'}})
        self.write('_receipt', 'new', {'signature': 's', 'format': 2, 'created_at': 1,
                                      'operation': 'cap.invoke', 'result': {'operation': 'b'}})
        self.write('actions', 'a', self.action())
        self.write('actions', 'b', self.action('b'))
        self.sweep()
        self.assertIn('result', self.get('_receipt', 'old'))
        self.assertIsNone(self.get('_receipt', 'new'))
        self.assertNotIn('result_expired', self.get('actions', 'a'))
        self.assertIsNone(self.get('actions', 'b'))

    def test_stream_receipts_are_roots_never_compacted(self):
        stream = {'id': 'slot', 'floor': 1, 'result': {'operation': 'a'}}
        self.write('_receipt_stream', 'slot', stream)
        self.write('actions', 'a', self.action())
        self.assertEqual(self.sweep(), 0)
        self.assertEqual(self.get('_receipt_stream', 'slot'), stream)

    def test_completed_submission_and_associated_mapping_deleted(self):
        self.write('_submissions', 'a', {'id': 'a', 'inputs': {'x': 'data'},
                                       'owner': 'admin', 'lease': 'l'})
        self.write('actions', 'a', self.action())
        self.store.execute(lambda tx: tx.put('_submission_leases', 'l', 'a'))
        self.store.execute(lambda tx: tx.put('leases', 'l', {'id': 'l', 'state': 'released', 'task': 'a'}))
        self.sweep()
        self.store.execute(lambda tx: retention.sweep(tx, now=200, seconds=10))
        self.assertIsNone(self.get('_submissions', 'a'))
        self.assertIsNone(self.get('actions', 'a'))
        self.assertIsNone(self.get('_submission_leases', 'l'))

    def test_inactive_roots_do_not_starve_unrelated_candidates(self):
        self.write('actions', 'a', self.action())
        self.write('actions', 'b', self.action('b'))
        self.store.execute(lambda tx: tx.put('work', 'w', {'refs': ['a']}))
        self.assertEqual(self.sweep(batch=1), 1)
        self.assertIsNone(self.get('actions', 'b'))

    def test_tracking_and_pruning_roll_back_atomically(self):
        def fail_write(tx):
            with retention.tracking(tx, 1):
                tx.put('actions', 'a', self.action())
            raise RuntimeError('abort')
        with self.assertRaises(RuntimeError):
            self.store.execute(fail_write)
        self.assertIsNone(self.get('actions', 'a'))
        self.assertIsNone(self.get('_retention', 'actions:a'))
        self.assertIsNone(self.get('_meta', retention.META))
        self.write('actions', 'a', self.action())
        before = self.get('actions', 'a')
        self.assertIn('expires_at', before)
        def fail_sweep(tx):
            retention.sweep(tx, now=100, seconds=10)
            raise RuntimeError('abort')
        with self.assertRaises(RuntimeError):
            self.store.execute(fail_sweep)
        self.assertEqual(self.get('actions', 'a'), before)
        self.assertIsNotNone(self.get('_retention', 'actions:a'))

    def test_durable_upload_overrides_transient_content(self):
        def put(tx, transient):
            with retention.tracking(tx, 1):
                return knowledge.artifact(tx, None, 'REFUQQ==', transient)
        ref = self.store.execute(lambda tx: put(tx, True))
        self.assertTrue(self.get('_retention', 'artifacts:' + ref['sha256'])['operational'])
        self.store.execute(lambda tx: put(tx, False))
        self.sweep()
        self.assertIsNotNone(self.get('artifacts', ref['sha256']))

    def test_legacy_artifact_never_enrolled_by_transient_upload(self):
        ref = self.store.execute(lambda tx: knowledge.artifact(tx, None, 'REFUQQ=='))
        def put(tx):
            with retention.tracking(tx, 1):
                knowledge.artifact(tx, None, 'REFUQQ==', True)
        self.store.execute(put)
        self.assertIsNone(self.get('_retention', 'artifacts:' + ref['sha256']))
        self.assertEqual(self.sweep(), 0)

    def test_event_uuid_reference_is_a_preservation_root(self):
        def add(tx):
            with retention.tracking(tx, 1):
                event = tx.emit('diagnostic', {})
            tx.put('revisions', 'rev', {'body': {'evidence_event': event['id']}})
        self.store.execute(add)
        self.assertEqual(self.sweep(), 0)
        self.assertIsNotNone(self.get('events', '%020d' % 1))

    def expired_terminal(self, evidence='evidence'):
        # A persisted summary from the previous version, before whole-row sweep.
        outcome={'stdout':'x'*100000,'stdout_artifact':'blob',evidence:{'quiescent':True,'detail':'y'*100000}}
        action=self.action();action['result']=outcome
        summary=retention.compact_action(action)
        summary.update(result_expired=True,expired_at=1)
        self.store.execute(lambda tx:tx.put('actions','a',summary))
        return action,outcome

    def test_legacy_summary_compatibility_before_whole_row_sweep(self):
        for evidence in ('evidence', 'reconciliation'):
            action, outcome = self.expired_terminal(evidence)
            summary = self.get('actions', 'a')
            self.assertTrue(summary['result_expired'])
            self.assertEqual(summary['identity_digest'], digest([
                action['contract'], action['inputs'], action['resource'],
                action['generation'], action['lease']]))
            self.assertEqual(summary['result_digest'], digest(outcome))
            self.assertEqual(summary['recovery_result'][evidence], {'quiescent': True})
            self.assertNotIn('stdout', encode(summary))
            self.assertLess(len(encode(summary)), 800)
            self.assertIsNone(self.get('artifacts', 'blob'))
            self.store.execute(lambda tx: tx.drop('actions', 'a'))

    def test_expired_exact_result_duplicate_accepts_lost_ack_without_mutation(self):
        action, outcome = self.expired_terminal()
        before = self.get('actions', 'a')
        def redeliver(tx):
            reply = providers.result(tx, api.Context(None), 'p', 'a', 1, 'succeeded', outcome)
            self.assertEqual(reply, before)
            self.assertEqual(tx.changes, {})
        self.store.execute(redeliver)
        self.assertEqual(self.get('actions', 'a'), before)
        self.assertIsNone(self.get('artifacts', 'blob'))

    def test_expired_result_conflict_provider_generation_and_state_rejected(self):
        action, outcome = self.expired_terminal()
        for provider, generation, state, result in (
                ('p', 1, 'succeeded', {'stdout': 'different'}),
                ('p', 1, 'failed', outcome), ('p', 2, 'succeeded', outcome),
                ('foreign', 1, 'succeeded', outcome)):
            with self.assertRaises(Fault) as error:
                self.store.execute(lambda tx: providers.result(
                    tx, api.Context(None), provider, 'a', generation, state, result))
            self.assertEqual(error.exception.status, 409)

    def test_expired_result_recovery_status_explicit_and_authorized(self):
        self.expired_terminal()
        summary = self.store.execute(lambda tx: providers.status(
            tx, api.Context(None), 'a', recovery=True), read_only=True)
        self.assertTrue(summary['recovery_result']['evidence']['quiescent'])
        with self.assertRaises(Fault) as error:
            self.store.execute(lambda tx: providers.status(
                tx, api.Context(None, actor='foreign', permissions=[]), 'a', recovery=True))
        self.assertEqual(error.exception.status, 403)
        with self.assertRaises(Fault) as error:
            self.store.execute(lambda tx: providers.status(tx, api.Context(None), 'a'))
        self.assertEqual(error.exception.status, 410)

    def test_expired_operation_cannot_be_dispatched_again(self):
        action, outcome = self.expired_terminal()
        self.store.execute(lambda tx: tx.put('leases', 'l', {'id': 'l', 'state': 'bound'}))
        with self.assertRaises(Fault) as error:
            self.store.execute(lambda tx: providers.dispatch(
                tx, api.Context(None), 'l', action['inputs'], 'a'))
        self.assertEqual(error.exception.status, 410)
        self.assertTrue(self.get('actions', 'a')['result_expired'])

    def test_incomplete_identity_is_preserved_without_blocking_valid_actions(self):
        malformed = {'id': 'broken', 'state': 'succeeded', 'result': {'value': 'retain'}}
        self.write('actions', 'broken', malformed)
        self.write('actions', 'a', self.action())
        for unused in range(10):
            self.sweep()
        self.assertEqual(self.get('actions', 'broken'), malformed)
        self.assertIsNone(self.get('actions', 'a'))


if __name__ == '__main__':
    unittest.main()
