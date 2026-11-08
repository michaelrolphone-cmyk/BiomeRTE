import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from biomerte.worker import Worker
from biomerte.store import encode, digest, Fault


class Remote:
    site, realm, node = 'site', 'site', 'provider'
    def call(self, name, args):
        if name == 'provider.claim':
            return {'state': 'claimed'}
        raise AssertionError('Acknowledged result must not be resubmitted')


class WorkerResultRetention(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'worker.sqlite')
        self.worker = Worker(self.path, Remote(), [])
        self.addCleanup(lambda: self.worker.db.close())

    def seed(self, identifier, result, state='succeeded', signature='signature', eligible=True):
        self.worker.db.execute('INSERT INTO actions VALUES(?,?,?,?,?)',
                               (identifier, signature, state, encode(result), 'resource'))
        if eligible:
            self.worker.db.execute('INSERT INTO result_retention_eligible VALUES(?,?)',
                                   (identifier, signature))

    def test_debug_count_and_byte_bounds_preserve_identity_fences(self):
        with patch('biomerte.worker.RESULT_DEBUG_COUNT', 3), patch('biomerte.worker.RESULT_DEBUG_BYTES', 50):
            for i in range(10):
                self.seed(str(i), {'value': 'x' * 10})
                self.worker.retire_result(str(i), 'signature', 'succeeded', {'value': 'x' * 10})
        self.assertEqual(10, self.worker.db.execute('SELECT COUNT(*) FROM actions').fetchone()[0])
        self.assertEqual(10, self.worker.db.execute('SELECT COUNT(*) FROM result_acknowledged').fetchone()[0])
        count, size = self.worker.db.execute('SELECT COUNT(*),SUM(bytes) FROM result_debug').fetchone()
        self.assertLessEqual(count, 3)
        self.assertLessEqual(size, 50)
        self.assertEqual([('{}',)], self.worker.db.execute('SELECT DISTINCT result FROM actions').fetchall())

    def test_uncertain_and_unacknowledged_results_remain(self):
        self.seed('uncertain', {'evidence': 'important'}, 'uncertain')
        self.seed('pending', {'evidence': 'pending acknowledgement'})
        with self.assertRaises(Fault):
            self.worker.retire_result('uncertain', 'signature', 'uncertain', {'evidence': 'important'})
        rows = self.worker.db.execute('SELECT id,result FROM actions ORDER BY id').fetchall()
        self.assertIn('pending acknowledgement', rows[0][1])
        self.assertIn('important', rows[1][1])
        self.assertEqual([], self.worker.db.execute('SELECT * FROM result_acknowledged').fetchall())

    def test_oversized_acknowledged_result_not_retained_in_debug(self):
        result = {'large': '\u2603' * 100}
        self.seed('large', result)
        with patch('biomerte.worker.RESULT_DEBUG_BYTES', 50):
            self.worker.retire_result('large', 'signature', 'succeeded', result)
        self.assertEqual([], self.worker.db.execute('SELECT * FROM result_debug').fetchall())
        self.assertEqual('{}', self.worker.db.execute('SELECT result FROM actions').fetchone()[0])

    def test_replayed_acknowledged_action_after_restart_never_executes_or_sends_empty_result(self):
        action = {'id': 'done', 'contract': 'test', 'inputs': {}, 'resource': 'resource',
                  'generation': 1, 'lease': 'lease'}
        signature = digest(['test', {}, 'resource', 1, 'lease'])
        self.seed('done', {'ok': True}, signature=signature)
        self.worker.db.execute('INSERT INTO fences VALUES(?,?,?)', ('resource', 1, 'lease'))
        self.worker.retire_result('done', signature, 'succeeded', {'ok': True})
        self.worker.db.close()
        self.worker = Worker(self.path, Remote(), [])
        with patch('biomerte.worker.subprocess.Popen', side_effect=AssertionError('Repeated effect')):
            self.worker.perform(action)
        action['inputs'] = {'changed': True}
        with self.assertRaises(Fault):
            self.worker.perform(action)


    def test_acknowledgement_cleanup_is_durable_and_retried_without_result_payload(self):
        self.seed('done', {'ok': True})
        output = os.path.join(self.tmp.name, 'cleanup.json')
        code = "import json,sys; data=json.load(sys.stdin); open(sys.argv[1],'w').write(json.dumps(data))"
        self.worker.retire_result('done', 'signature', 'succeeded', {'ok': True},
                                  [sys.executable, '-c', code, output])
        self.worker.db.close()
        self.worker = Worker(self.path, Remote(), [])
        self.worker.cleanup_results()
        import json
        with open(output) as handle:
            self.assertEqual({'operation': 'done', 'result_digest': digest({'ok': True})}, json.load(handle))
        self.assertEqual([], self.worker.db.execute('SELECT * FROM result_cleanup').fetchall())
        self.assertEqual('{}', self.worker.db.execute('SELECT result FROM actions').fetchone()[0])

    def test_failed_cleanup_preserves_acknowledged_fence_and_pending_cleanup(self):
        self.seed('done', {'ok': True})
        self.worker.retire_result('done', 'signature', 'succeeded', {'ok': True},
                                  [sys.executable, '-c', 'raise SystemExit(1)'])
        with self.assertRaises(Fault):
            self.worker.cleanup_results()
        self.assertEqual(1, self.worker.db.execute('SELECT COUNT(*) FROM result_cleanup').fetchone()[0])
        self.assertEqual(1, self.worker.db.execute('SELECT COUNT(*) FROM result_acknowledged').fetchone()[0])
        self.assertEqual('{}', self.worker.db.execute('SELECT result FROM actions').fetchone()[0])


    def test_legacy_redelivery_preserves_payload_and_never_enqueues_cleanup(self):
        action = {'id': 'legacy', 'contract': 'test', 'inputs': {}, 'resource': 'resource',
                  'generation': 1, 'lease': 'lease'}
        signature = digest(['test', {}, 'resource', 1, 'lease'])
        original = {'historical': 'keep this full result'}
        self.seed('legacy', original, signature=signature, eligible=False)
        self.worker.db.execute('INSERT INTO fences VALUES(?,?,?)', ('resource', 1, 'lease'))
        self.worker.offers = [{'contract': 'test', 'resource': 'resource',
                               'acknowledge_argv': [sys.executable, '-c', 'raise SystemExit(99)']}]
        def call(name, args):
            if name == 'provider.claim':
                return {'state': 'claimed'}
            self.assertEqual('provider.result', name)
            return dict(args, id=args['operation'])
        with patch.object(self.worker.remote, 'call', side_effect=call), patch(
                'biomerte.worker.subprocess.Popen', side_effect=AssertionError('Unexpected execution')):
            self.worker.perform(action)
        self.assertEqual(encode(original), self.worker.db.execute('SELECT result FROM actions').fetchone()[0])
        for table in ('result_debug', 'result_acknowledged', 'result_cleanup', 'result_retention_eligible'):
            self.assertEqual([], self.worker.db.execute('SELECT * FROM ' + table).fetchall())

    def test_ack_transaction_failure_rolls_back_debug_marker_cleanup_and_payload(self):
        self.seed('done', {'ok': True})
        self.worker.db.execute("CREATE TRIGGER refuse_clear BEFORE UPDATE ON actions BEGIN "
                               "SELECT RAISE(ABORT, 'injected failure'); END")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.worker.retire_result('done', 'signature', 'succeeded', {'ok': True},
                                      [sys.executable, '-c', 'pass'])
        self.assertFalse(self.worker.db.in_transaction)
        self.assertEqual(encode({'ok': True}), self.worker.db.execute('SELECT result FROM actions').fetchone()[0])
        for table in ('result_debug', 'result_acknowledged', 'result_cleanup'):
            self.assertEqual([], self.worker.db.execute('SELECT * FROM ' + table).fetchall())

    def test_new_action_and_eligibility_are_atomic_before_adapter_execution(self):
        action = {'id': 'new', 'contract': 'test', 'inputs': {}, 'resource': 'resource',
                  'generation': 1, 'lease': 'lease'}
        self.worker.db.execute('INSERT INTO fences VALUES(?,?,?)', ('resource', 1, 'lease'))
        self.worker.offers = [{'contract': 'test', 'resource': 'resource',
                               'argv': [sys.executable, '-c', 'print("{}")']}]
        self.worker.db.execute("CREATE TRIGGER refuse_eligibility BEFORE INSERT ON result_retention_eligible "
                               "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        import sqlite3
        with patch('biomerte.worker.subprocess.Popen', side_effect=AssertionError('Effect before commit')):
            with self.assertRaises(sqlite3.IntegrityError):
                self.worker.perform(action)
        self.assertFalse(self.worker.db.in_transaction)
        self.assertEqual([], self.worker.db.execute('SELECT * FROM actions').fetchall())
        self.assertEqual([], self.worker.db.execute('SELECT * FROM result_retention_eligible').fetchall())

    def test_unmarked_pending_cleanup_is_not_executed(self):
        self.seed('legacy', {}, eligible=False)
        self.worker.db.execute('INSERT INTO result_cleanup VALUES(?,?,?)',
                               ('legacy', encode([sys.executable, '-c', 'pass']), '{}'))
        with patch('biomerte.worker.subprocess.Popen', side_effect=AssertionError('Legacy cleanup')):
            self.worker.cleanup_results()
        self.assertEqual(1, self.worker.db.execute('SELECT COUNT(*) FROM result_cleanup').fetchone()[0])


    def test_cleanup_has_bounded_timeout_when_worker_execution_timeout_is_none(self):
        self.worker.timeout = None
        self.seed('done', {'ok': True})
        self.worker.retire_result('done', 'signature', 'succeeded', {'ok': True},
                                  [sys.executable, '-c', 'import json,sys; json.load(sys.stdin)'])
        self.worker.cleanup_results()
        self.assertEqual([], self.worker.db.execute('SELECT * FROM result_cleanup').fetchall())
        self.assertEqual([('done',)], self.worker.db.execute('SELECT * FROM result_acknowledged').fetchall())
