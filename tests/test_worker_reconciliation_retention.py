"""Bound redundant audit payloads without weakening local execution receipts."""
import copy
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from biomerte.store import Fault, digest, encode
from biomerte.worker import Worker, reconcile_receipt


class Remote:
    site, realm, node = 'site', 'site', 'provider'

    def __init__(self):
        self.records, self.calls = {}, []
        self.offline = False
        self.lose_ack = False

    def call(self, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        if self.offline:
            raise OSError('Disconnected')
        if name == 'cap.status':
            return copy.deepcopy(self.records[args['operation']])
        if name == 'provider.claim':
            return {'state': 'claimed'}
        if name == 'provider.result':
            record = self.records[args['operation']]
            record.update(state=args['state'], result=copy.deepcopy(args['result']))
            if self.lose_ack:
                self.lose_ack = False
                raise OSError('Lost acknowledgement')
            return copy.deepcopy(record)
        raise AssertionError(name)


class ReconciliationRetention(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'worker.sqlite')
        self.remote = Remote()
        self.worker = Worker(self.path, self.remote, [])
        self.addCleanup(lambda: self.worker.db.close())

    def seed(self, identifier='op', previous=None, result=None, eligible=True, expired=False):
        previous = previous if previous is not None else {'reason': 'inspect physical effect'}
        result = result if result is not None else {'stdout': 'response', 'evidence': {'quiescent': True}}
        action = dict(id=identifier, provider=self.remote.node, contract='test:1', inputs={},
                      resource='resource', guard='guard-' + identifier, generation=1, lease='lease',
                      state='succeeded', result=result)
        signature = digest([action[k] for k in ('contract', 'inputs', 'resource', 'generation', 'lease')])
        self.worker.db.execute('INSERT INTO actions VALUES(?,?,?,?,?)',
                               (identifier, signature, 'uncertain', encode(previous), action['guard']))
        if eligible:
            self.worker.db.execute('INSERT INTO result_retention_eligible VALUES(?,?)', (identifier, signature))
        self.worker.db.execute('INSERT INTO fences VALUES(?,?,?)', (action['guard'], 1, 'lease'))
        if expired:
            action.update(result_expired=True, identity_digest=signature, result_digest=digest(result),
                          recovery_result={'result_expired': True, 'result_digest': digest(result),
                                           'evidence': {'quiescent': True}})
            action.pop('result')
        self.remote.records[identifier] = action
        return action, signature, previous, result

    def reconcile(self, action):
        self.assertTrue(self.worker.reconcile_guard(action['guard']))

    def row(self, table, identifier='op'):
        return self.worker.db.execute('SELECT * FROM ' + table + ' WHERE id=?', (identifier,)).fetchone()

    def assert_compact(self, identifier, previous_body, result_body):
        row = self.row('reconciliations', identifier)
        for body, summary in ((previous_body, row[1]), (result_body, row[3])):
            self.assertEqual(json.loads(summary), dict(payload_expired=True,
                payload_bytes=len(body.encode('utf-8')),
                payload_sha256=hashlib.sha256(body.encode('utf-8')).hexdigest()))
        self.assertIsNotNone(self.row('reconciliation_retention_eligible', identifier)[4])

    def test_large_both_payloads_compact_to_exact_digests_after_ack(self):
        a, signature, previous, result = self.seed(previous={'old': 'x' * 1100000},
            result={'new': '\u2603' * 200000, 'evidence': {'quiescent': True}})
        # Preserve byte identity even when an older writer used noncanonical JSON.
        old_body = json.dumps(previous, indent=1)
        self.worker.db.execute('UPDATE actions SET result=? WHERE id=?', (old_body, a['id']))
        self.reconcile(a)
        observed = self.row('reconciliations')[4]
        self.assertEqual(self.row('reconciliations')[1:4], (old_body, 'succeeded', encode(result)))
        self.assertEqual(self.row('reconciliation_retention_eligible')[1:4],
                         (signature, 'controller_result', digest(result)))
        self.worker.recover_result_acknowledgements()
        self.assert_compact('op', old_body, encode(result))
        self.assertEqual(observed, self.row('reconciliations')[4])
        self.assertEqual('{}', self.row('actions')[3])
        self.assertEqual(signature, self.row('actions')[1])
        self.assertEqual([], self.worker.db.execute('SELECT * FROM reconciliation_debug').fetchall())
        self.assertEqual([], self.worker.db.execute('SELECT * FROM result_debug').fetchall())
        self.assertEqual([], self.worker.db.execute('SELECT * FROM result_recovery_pending').fetchall())

    def test_recent_pairs_obey_count_and_combined_encoded_byte_bounds(self):
        for count, budget, expected in ((3, 100000, 3), (100, 150, 1)):
            with patch('biomerte.worker.RECONCILIATION_DEBUG_COUNT', count), patch(
                    'biomerte.worker.RECONCILIATION_DEBUG_BYTES', budget):
                for i in range(10):
                    identifier = '%d-%d' % (count, i)
                    a, signature, previous, result = self.seed(identifier)
                    self.reconcile(a)
                    self.worker.retire_result(identifier, signature, 'succeeded', result)
                rows = self.worker.db.execute('SELECT id,previous_result,result,bytes FROM reconciliation_debug').fetchall()
                self.assertEqual(expected, len(rows))
                self.assertLessEqual(sum(r[3] for r in rows), budget)
                for r in rows:
                    self.assertEqual(r[3], sum(len(x.encode('utf-8')) for x in r[1:3]))
                self.assertIn(identifier, [r[0] for r in rows])
        self.assertEqual(20, self.worker.db.execute('SELECT COUNT(*) FROM reconciliations').fetchone()[0])

    def test_legacy_action_and_preexisting_audit_payloads_never_enroll(self):
        for action_marked in (False, True):
            identifier = str(action_marked)
            a, signature, previous, result = self.seed(identifier, eligible=action_marked)
            if action_marked:
                self.worker.db.execute('INSERT INTO reconciliations VALUES(?,?,?,?,?)',
                    (identifier, 'legacy previous bytes', 'succeeded', 'legacy result bytes', 1))
            self.reconcile(a)
            before = self.row('reconciliations', identifier)
            self.worker.recover_result_acknowledgements()
            self.assertEqual(before, self.row('reconciliations', identifier))
            self.assertIsNone(self.row('reconciliation_retention_eligible', identifier))
        self.worker.db.close()
        self.worker = Worker(self.path, self.remote, [])
        self.assertEqual([], self.worker.db.execute('SELECT * FROM reconciliation_retention_eligible').fetchall())

    def test_eligibility_signature_mismatch_does_not_enroll_audit(self):
        a, signature, previous, result = self.seed()
        self.worker.db.execute("UPDATE result_retention_eligible SET signature='wrong'")
        self.reconcile(a)
        self.assertIsNone(self.row('reconciliation_retention_eligible'))
        self.assertFalse(self.worker.retire_result('op', signature, 'succeeded', result))
        self.assertEqual(encode(previous), self.row('reconciliations')[1])

    def test_delayed_ack_and_offline_restart_preserve_all_pending_copies(self):
        a, signature, previous, result = self.seed()
        self.reconcile(a)
        saved = self.row('reconciliations')
        self.worker.db.close()
        self.worker = Worker(self.path, self.remote, [])
        self.remote.offline = True
        self.worker.recover_result_acknowledgements()
        self.assertEqual(saved, self.row('reconciliations'))
        self.assertEqual(encode(result), self.row('actions')[3])
        self.assertIsNone(self.row('result_acknowledged'))
        self.assertEqual(('op', signature), self.row('result_recovery_pending'))
        self.remote.offline = False
        self.worker.recover_result_acknowledgements()
        self.assert_compact('op', encode(previous), encode(result))

    def test_unresolved_nonquiescent_and_changed_receipts_never_compact(self):
        a, signature, previous, result = self.seed()
        a['result']['evidence']['quiescent'] = False
        self.assertFalse(self.worker.reconcile_guard(a['guard']))
        self.assertIsNone(self.row('reconciliations'))
        self.assertEqual('uncertain', self.row('actions')[2])
        a['result']['evidence']['quiescent'] = True
        original = self.remote.call
        def concurrent_change(name, args):
            response = original(name, args)
            self.worker.db.execute("UPDATE actions SET result='{}' WHERE id='op'")
            return response
        with patch.object(self.remote, 'call', side_effect=concurrent_change):
            self.assertFalse(self.worker.reconcile_guard(a['guard']))
        self.assertIsNone(self.row('reconciliations'))

    def test_expired_authority_compacts_audit_atomically_without_resubmission(self):
        a, signature, previous, result = self.seed(expired=True)
        self.reconcile(a)
        self.assert_compact('op', encode(previous), encode(a['recovery_result']))
        marker = self.row('reconciliation_retention_eligible')
        self.assertEqual(marker[1:4], (signature, 'controller_recovery', digest(result)))
        self.assertEqual(encode(a['recovery_result']), self.row('actions')[3])
        self.assertIsNotNone(self.row('result_acknowledged'))
        self.assertIsNone(self.row('result_recovery_pending'))
        self.worker.perform(a)
        self.assertFalse(any(name == 'provider.result' for name, args in self.remote.calls))

    def test_repeated_reconciliation_and_acknowledged_replay_do_not_refresh_debug(self):
        a, signature, previous, result = self.seed()
        self.reconcile(a)
        self.worker.retire_result('op', signature, 'succeeded', result)
        audit = self.row('reconciliations')
        debug = self.row('reconciliation_debug')
        self.worker.db.close()
        self.worker = Worker(self.path, self.remote, [])
        with patch('biomerte.worker.subprocess.Popen', side_effect=AssertionError('Repeated effect')):
            self.reconcile(a)
            self.worker.perform(a)
            self.worker.recover_result_acknowledgements()
        self.assertEqual(audit, self.row('reconciliations'))
        self.assertEqual(debug, self.row('reconciliation_debug'))
        self.assertFalse(any(name == 'provider.result' for name, args in self.remote.calls))

    def test_compaction_failure_rolls_back_ack_action_debug_and_pending_projection(self):
        a, signature, previous, result = self.seed()
        self.reconcile(a)
        before = self.row('reconciliations')
        self.worker.db.execute("CREATE TRIGGER fail_compaction BEFORE UPDATE ON reconciliations "
                               "BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.worker.retire_result('op', signature, 'succeeded', result, [sys.executable, '-c', 'pass'])
        self.assertFalse(self.worker.db.in_transaction)
        self.assertEqual(before, self.row('reconciliations'))
        self.assertEqual(encode(result), self.row('actions')[3])
        self.assertEqual(('op', signature), self.row('result_recovery_pending'))
        self.assertIsNone(self.row('reconciliation_retention_eligible')[4])
        for table in ('result_debug', 'reconciliation_debug', 'result_cleanup', 'result_acknowledged'):
            self.assertIsNone(self.row(table))

    def test_expired_compaction_failure_rolls_back_adoption_and_enrollment(self):
        a, signature, previous, result = self.seed(expired=True)
        self.worker.db.execute("CREATE TRIGGER fail_compaction BEFORE UPDATE ON reconciliations "
                               "BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.worker.reconcile_guard(a['guard'])
        self.assertEqual(('uncertain', encode(previous)), self.row('actions')[2:4])
        for table in ('reconciliations', 'reconciliation_retention_eligible', 'reconciliation_debug',
                      'result_acknowledged', 'result_recovery_pending'):
            self.assertIsNone(self.row(table))

    def test_process_crash_during_compaction_recovers_original_transaction(self):
        a, signature, previous, result = self.seed()
        self.reconcile(a)
        code = '''import os,sys,json
from biomerte.worker import Worker
class Remote:
    site,realm,node='site','site','provider'
w=Worker(sys.argv[1], Remote(), [])
w.db.create_function('crash_now',0,lambda: os._exit(73))
w.db.execute("CREATE TEMP TRIGGER crash_compaction BEFORE UPDATE ON reconciliations BEGIN SELECT crash_now(); END")
w.retire_result('op',sys.argv[2],'succeeded',json.loads(sys.argv[3]))
'''
        child = subprocess.Popen([sys.executable, '-c', code, self.path, signature, encode(result)])
        self.assertEqual(73, child.wait(timeout=20))
        self.worker.db.close()
        self.worker = Worker(self.path, self.remote, [])
        self.assertEqual(encode(previous), self.row('reconciliations')[1])
        self.assertEqual(encode(result), self.row('actions')[3])
        self.assertIsNone(self.row('result_acknowledged'))
        self.assertEqual(('op', signature), self.row('result_recovery_pending'))
        self.worker.recover_result_acknowledgements()
        self.assert_compact('op', encode(previous), encode(result))

    def test_operator_exact_acknowledged_retries_never_rehydrate(self):
        for expired in (False, True):
            identifier = str(expired)
            a, signature, previous, result = self.seed(identifier, expired=expired)
            self.reconcile(a)
            if not expired:
                self.worker.recover_result_acknowledgements()
            before = self.row('actions', identifier)
            answer = reconcile_receipt(self.tmp.name, self.remote, identifier, 'succeeded', result)
            self.assertEqual(answer, a)
            self.assertEqual(before, self.row('actions', identifier))
            with self.assertRaises(Fault):
                reconcile_receipt(self.tmp.name, self.remote, identifier, 'succeeded', {'changed': True})
        self.assertFalse(any(name == 'provider.result' for name, args in self.remote.calls))

    def test_foreign_operation_and_malformed_expired_confirmation_are_rejected(self):
        a, signature, previous, result = self.seed(expired=True)
        a['id'] = 'another-operation'
        with self.assertRaises(Fault):
            self.worker.reconcile_guard(a['guard'])
        self.assertIsNone(self.row('reconciliations'))
        a['id'] = 'op'
        a['result'] = result
        a['result_digest'] = 'wrong'
        with self.assertRaises(Fault):
            reconcile_receipt(self.tmp.name, self.remote, 'op', 'succeeded', result)
        self.assertEqual(('uncertain', encode(previous)), self.row('actions')[2:4])
        self.assertIsNone(self.row('result_acknowledged'))

    def test_operator_expired_full_outcome_waits_for_configured_cleanup(self):
        a, signature, previous, result = self.seed(expired=True)
        target = os.path.join(self.tmp.name, 'cleanup.json')
        cleanup = [sys.executable, '-c',
                   'import sys;open(sys.argv[1],"w").write(sys.stdin.read())', target]
        self.worker.offers = [dict(contract='test:1', resource='resource', acknowledge_argv=cleanup)]
        reconcile_receipt(self.tmp.name, self.remote, 'op', 'succeeded', result)
        self.assertIsNone(self.row('result_acknowledged'))
        self.assertEqual(('op', signature), self.row('result_recovery_pending'))
        self.worker.recover_result_acknowledgements()
        with open(target) as stream:
            self.assertEqual(json.load(stream), {'operation': 'op', 'result_digest': digest(result)})
        self.assertEqual('{}', self.row('actions')[3])
        self.assertIsNotNone(self.row('result_acknowledged'))
        self.assertIsNone(self.row('result_cleanup'))
        self.assertFalse(any(name == 'provider.result' for name, args in self.remote.calls))

    def test_operator_lost_ack_retains_uncertainty_then_recovers_exact_outcome(self):
        a, signature, previous, result = self.seed()
        self.remote.lose_ack = True
        with self.assertRaises(OSError):
            reconcile_receipt(self.tmp.name, self.remote, 'op', 'succeeded', result)
        self.assertEqual(('uncertain', encode(previous)), self.row('actions')[2:4])
        self.assertIsNone(self.row('result_acknowledged'))
        reconcile_receipt(self.tmp.name, self.remote, 'op', 'succeeded', result)
        self.assertEqual(('op', signature), self.row('result_recovery_pending'))
        self.worker.recover_result_acknowledgements()
        self.assertEqual('{}', self.row('actions')[3])
        self.assertIsNotNone(self.row('result_acknowledged'))

    def test_idle_poll_does_not_scan_completed_reconciliation_history(self):
        a, signature, previous, result = self.seed()
        self.reconcile(a)
        self.worker.recover_result_acknowledgements()
        # Fixture seeding is one durable transaction, not 10,000 fsyncs.
        self.worker.db.execute('BEGIN')
        try:
            self.worker.db.executemany('INSERT INTO reconciliations VALUES(?,?,?,?,?)',
                [('history-%d' % i, '{}', 'succeeded', '{}', 1) for i in range(10000)])
            self.worker.db.commit()
        except Exception:
            self.worker.db.rollback()
            raise
        calls = len(self.remote.calls)
        instructions = []
        def bounded_poll():
            instructions.append(1)
            return int(len(instructions) > 100)
        self.worker.db.set_progress_handler(bounded_poll, 100)
        try:
            self.worker.recover_result_acknowledgements()
            self.worker.cleanup_results()
        finally:
            self.worker.db.set_progress_handler(None, 0)
        self.assertEqual(calls, len(self.remote.calls))
        self.assertLessEqual(len(instructions), 100)
