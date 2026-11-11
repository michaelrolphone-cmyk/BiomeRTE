"""Lost ACKs after action expiry must never re-execute or restore bulk output."""
import tempfile
import unittest
from unittest.mock import patch
from biomerte.app import Application, initialize
from biomerte.store import Fault, uid, digest


class CompletionReceipts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        initialize(self.tmp.name)
        self.app = Application(self.tmp.name)
        self.now = 10000.
        clock = patch('time.time', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.call('cap.define', name='host.terminal.run', inputs={}, outputs={})
        self.call('cap.offer', provider='test', contract='host.terminal.run:1', resource='slot',
                  configuration={'lease_class':'command'})
        lease = self.call('cap.acquire', task='one', contract='host.terminal.run:1', resource='slot')
        self.call('cap.fenced', provider='test', lease=lease['id'], generation=lease['generation'])
        self.action = self.call('cap.invoke', lease=lease['id'], inputs={}, operation='one')
        self.args = dict(provider='test', operation='one', generation=lease['generation'], state='failed', result={'exit_code':1,'evidence':{'quiescent':True}})
        self.call('provider.claim', provider='test', operation='one', generation=lease['generation'])
        self.call('provider.result', **self.args)

    def call(self, op, **arguments):
        return self.app.call(op, arguments, uid())

    def expire_action(self):
        self.now += 901
        self.app.tick('site')
        with self.assertRaises(Fault):
            self.call('cap.status', operation='one')

    def test_exact_lost_ack_after_sweep_and_restart_returns_only_digests(self):
        self.expire_action()
        self.app = Application(self.tmp.name)
        ack = self.call('provider.result', **self.args)
        self.assertTrue(ack['acknowledged'])
        self.assertTrue(ack['result_expired'])
        self.assertEqual(ack['result_digest'], digest(self.args['result']))
        self.assertEqual(ack['identity_digest'], digest([self.action[k] for k in ('contract','inputs','resource','generation','lease')]))
        self.assertNotIn('result', ack)
        self.assertEqual(ack['recovery_result']['evidence'], {'quiescent':True})
        with self.app.stores['site'].connect() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM records WHERE kind='actions'").fetchone()[0])
        self.assertEqual(ack, self.call('cap.status', operation='one', recovery=True))

    def test_receipt_is_not_a_payload_retention_root(self):
        from biomerte.retention_index import advance, reference_hash
        with self.app.stores['site'].connect() as db:
            for _ in range(100):
                if advance(db):
                    break
            row = db.execute("SELECT ignored FROM retention_ref_sources WHERE kind='_completion_receipts' AND id='one'").fetchone()
            self.assertEqual(row, (1,))

    def test_conflicting_result_provider_or_generation_cannot_be_acknowledged(self):
        self.expire_action()
        for change in ({'result':{'exit_code':0}}, {'generation':self.args['generation']+1}, {'provider':'other'}, {'state':'succeeded'}):
            with self.assertRaises(Fault):
                self.call('provider.result', **dict(self.args, **change))

    def test_retries_do_not_extend_fixed_receipt_expiry(self):
        self.expire_action()
        first = self.call('provider.result', **self.args)
        self.now += 100
        self.assertEqual(first['expires_at'], self.call('provider.result', **self.args)['expires_at'])
        self.now = first['expires_at'] + 1
        self.app.tick('site')
        missing = self.call('cap.status', operation='one', recovery=True)
        self.assertEqual(missing['state'], 'missing_unknown')
        self.assertFalse(missing['acknowledged'])
        self.assertFalse(missing['replay_allowed'])
        with self.assertRaises(Fault):
            self.call('provider.result', **self.args)


if __name__ == '__main__':
    unittest.main()
