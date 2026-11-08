import copy
import os
import tempfile
import unittest
from unittest.mock import patch
from biomerte.store import Fault, digest, encode
from biomerte.worker import Worker, reconcile_receipt


class RecoveryRemote:
    site, realm, node = 'site', 'site', 'provider'
    def __init__(self, action, result):
        self.result = result
        self.summary = dict(action, provider=self.node, state='succeeded', result_expired=True,
                            identity_digest=digest([action[k] for k in ('contract','inputs','resource','generation','lease')]),
                            result_digest=digest(result), recovery_result={
                                'result_expired': True, 'result_digest': digest(result), 'evidence': {'quiescent': True}})
        self.summary.pop('inputs')
        self.calls = []
    def call(self, name, args):
        self.calls.append((name, args))
        if name == 'cap.status':
            if args != {'operation': self.summary['id'], 'recovery': True}:
                raise AssertionError('Recovery read required')
            return copy.deepcopy(self.summary)
        if name == 'provider.claim':
            return {'state': 'claimed'}
        if name == 'provider.result':
            if args['result'] != self.result:
                raise AssertionError('Shortened result resubmitted')
            return copy.deepcopy(self.summary)
        raise AssertionError(name)


class ExpiredRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.action = {'id': 'op', 'contract': 'test:1', 'inputs': {}, 'resource': 'resource',
                       'guard': 'resource', 'generation': 1, 'lease': 'lease'}
        self.result = {'stdout': 'large output', 'evidence': {'quiescent': True}}
        self.remote = RecoveryRemote(self.action, self.result)
        self.worker = Worker(os.path.join(self.tmp.name, 'worker.sqlite'), self.remote, [])
        self.addCleanup(lambda: self.worker.db.close())
        self.signature = self.remote.summary['identity_digest']
        self.worker.db.execute('INSERT INTO actions VALUES(?,?,?,?,?)',
                               ('op', self.signature, 'uncertain', encode({'reason': 'lost acknowledgement'}), 'resource'))
        self.worker.db.execute('INSERT INTO fences VALUES(?,?,?)', ('resource', 1, 'lease'))

    def test_expired_quiescent_summary_reconciles_and_cannot_be_resubmitted(self):
        self.assertTrue(self.worker.reconcile_guard('resource'))
        self.assertEqual('succeeded', self.worker.db.execute('SELECT state FROM actions').fetchone()[0])
        self.assertEqual([('op',)], self.worker.db.execute('SELECT * FROM result_acknowledged').fetchall())
        self.worker.db.close()
        self.worker = Worker(os.path.join(self.tmp.name, 'worker.sqlite'), self.remote, [])
        with patch('biomerte.worker.subprocess.Popen', side_effect=AssertionError('Effect replay')):
            self.worker.perform(self.action)
        self.assertFalse(any(name == 'provider.result' for name, args in self.remote.calls))

    def test_wrong_identity_and_nonquiescent_summary_do_not_resolve_uncertainty(self):
        self.remote.summary['identity_digest'] = 'wrong'
        with self.assertRaises(Fault):
            self.worker.reconcile_guard('resource')
        self.remote.summary['identity_digest'] = self.signature
        self.remote.summary['recovery_result'].pop('evidence')
        self.assertFalse(self.worker.reconcile_guard('resource'))
        self.assertEqual('uncertain', self.worker.db.execute('SELECT state FROM actions').fetchone()[0])

    def test_operator_exact_full_outcome_uses_expired_confirmation_without_resubmit(self):
        result = reconcile_receipt(self.tmp.name, self.remote, 'op', 'succeeded', self.result)
        self.assertTrue(result['result_expired'])
        self.assertFalse(any(name == 'provider.result' for name, args in self.remote.calls))
        self.assertEqual([('op',)], self.worker.db.execute('SELECT * FROM result_acknowledged').fetchall())

    def test_operator_shortened_or_mismatching_outcome_is_rejected(self):
        with self.assertRaises(Fault):
            reconcile_receipt(self.tmp.name, self.remote, 'op', 'succeeded', self.remote.summary['recovery_result'])
        self.assertEqual('uncertain', self.worker.db.execute('SELECT state FROM actions').fetchone()[0])
        self.assertFalse(any(name == 'provider.result' for name, args in self.remote.calls))

    def test_lost_ack_full_outcome_retry_accepts_verified_expired_ack(self):
        self.worker.db.execute('UPDATE actions SET state=?,result=? WHERE id=?',
                               ('succeeded', encode(self.result), 'op'))
        self.worker.db.execute('INSERT INTO result_retention_eligible VALUES(?,?)', ('op', self.signature))
        with patch('biomerte.worker.subprocess.Popen', side_effect=AssertionError('Effect replay')):
            self.worker.perform(self.action)
        self.assertEqual('{}', self.worker.db.execute('SELECT result FROM actions').fetchone()[0])
        self.assertEqual([('op',)], self.worker.db.execute('SELECT * FROM result_acknowledged').fetchall())
        self.assertEqual(1, sum(name == 'provider.result' for name, args in self.remote.calls))

    def test_summary_and_marker_are_atomic(self):
        self.worker.db.execute("CREATE TRIGGER fail_ack BEFORE INSERT ON result_acknowledged "
                               "BEGIN SELECT RAISE(ABORT, 'failure'); END")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.worker.reconcile_guard('resource')
        self.assertEqual('uncertain', self.worker.db.execute('SELECT state FROM actions').fetchone()[0])
        self.assertEqual([], self.worker.db.execute('SELECT * FROM result_acknowledged').fetchall())


    def test_once_recovers_lost_ack_without_server_repolling_completed_action(self):
        self.worker.db.execute('UPDATE actions SET state=?,result=? WHERE id=?',
                               ('succeeded', encode(self.result), 'op'))
        self.worker.db.execute('INSERT INTO result_retention_eligible VALUES(?,?)', ('op', self.signature))
        original = self.remote.call
        def call(name, args):
            if name == 'provider.poll':
                return {'actions': [], 'fences': []}
            return original(name, args)
        with patch.object(self.remote, 'call', side_effect=call), patch(
                'biomerte.worker.subprocess.Popen', side_effect=AssertionError('Effect replay')):
            self.worker.once()
        self.assertEqual('{}', self.worker.db.execute('SELECT result FROM actions').fetchone()[0])
        self.assertEqual([('op',)], self.worker.db.execute('SELECT * FROM result_acknowledged').fetchall())
        self.assertFalse(any(name == 'provider.result' for name, args in self.remote.calls))

    def test_ack_recovery_does_not_touch_legacy_or_mismatched_terminal_results(self):
        self.worker.db.execute('UPDATE actions SET state=?,result=? WHERE id=?',
                               ('succeeded', encode(self.result), 'op'))
        self.worker.recover_result_acknowledgements()
        self.assertEqual([], self.remote.calls)
        self.worker.db.execute('INSERT INTO result_retention_eligible VALUES(?,?)', ('op', self.signature))
        self.remote.summary['result_digest'] = 'wrong'
        self.worker.recover_result_acknowledgements()
        self.assertEqual(encode(self.result), self.worker.db.execute('SELECT result FROM actions').fetchone()[0])
        self.assertEqual([], self.worker.db.execute('SELECT * FROM result_acknowledged').fetchall())

    def test_recovery_is_bounded_and_bad_receipts_do_not_starve_later_receipts(self):
        for i in range(20):
            identifier = 'pending-%02d' % i
            self.worker.db.execute('INSERT INTO actions VALUES(?,?,?,?,?)',
                                   (identifier, 'signature', 'succeeded', '{}', 'resource'))
            self.worker.db.execute('INSERT INTO result_retention_eligible VALUES(?,?)', (identifier, 'signature'))
        seen = []
        def unavailable(name, args):
            seen.append(args['operation'])
            raise Fault('Unavailable', 503)
        with patch.object(self.remote, 'call', side_effect=unavailable):
            self.worker.recover_result_acknowledgements()
            self.assertEqual(8, len(seen))
            self.worker.recover_result_acknowledgements()
            self.assertEqual(16, len(set(seen)))
            self.worker.recover_result_acknowledgements()
            self.assertEqual(20, len(set(seen)))
