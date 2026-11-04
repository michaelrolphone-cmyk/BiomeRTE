import tempfile
import unittest
from unittest.mock import patch
from biomerte.app import Application, initialize
from biomerte.store import Fault, uid


class ReservationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        initialize(self.tmp.name)
        self.app = Application(self.tmp.name)
        self.clock = patch('biomerte.providers.time.time', return_value=1000)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.call('cap.define', dict(name='test.chat', inputs={'value': 'str'}, outputs={'value': 'str'}))
        self.policy = {'idle_seconds': 120, 'release_after_result': True}
        self.offer(self.policy)

    def call(self, op, args):
        return self.app.call(op, args, uid())

    def offer(self, policy):
        return self.call('cap.offer', dict(provider='provider', contract='test.chat:1',
            resource='chat', capacity=1, configuration={'reservation_policy': policy}))

    def acquire(self, task):
        return self.call('cap.acquire', dict(task=task, contract='test.chat:1', resource='chat'))

    def bound(self, lease):
        return self.call('cap.fenced', dict(provider='provider', lease=lease['id'], generation=lease['generation']))

    def inspect(self, lease):
        # Inspect via list so verification does not act as a caller keepalive.
        return next(l for l in self.call('cap.leases', {}) if l['id'] == lease['id'])

    def test_abandoned_unused_reservation_releases_and_promotes_without_client(self):
        first = self.acquire('abandoned')
        self.bound(first)
        second = self.acquire('waiting')
        self.now.return_value = 1119
        self.app.tick('site')
        self.assertEqual('bound', self.inspect(first)['state'])
        self.now.return_value = 1121
        self.app.tick('site')
        self.assertEqual('released', self.inspect(first)['state'])
        self.assertEqual('fencing', self.inspect(second)['state'])
        with self.assertRaises(Fault):
            self.call('cap.invoke', dict(lease=first['id'], operation='late', inputs={'value': 'x'}))

    def test_live_caller_poll_renews_unused_reservation(self):
        lease = self.acquire('live')
        self.now.return_value = 1110
        self.call('cap.lease', {'lease': lease['id']})
        self.now.return_value = 1130
        self.app.tick('site')
        self.assertEqual('fencing', self.inspect(lease)['state'])
        self.now.return_value = 1231
        self.app.tick('site')
        self.assertEqual('released', self.inspect(lease)['state'])

    def test_submitted_queued_claimed_and_uncertain_work_has_no_lifetime_limit(self):
        lease = self.acquire('work')
        self.call('cap.invoke', dict(lease=lease['id'], operation='job', inputs={'value': 'ciphertext'}))
        self.now.return_value = 1000000
        self.app.tick('site')
        self.assertEqual('fencing', self.inspect(lease)['state'])
        self.bound(lease)
        self.call('provider.claim', dict(provider='provider', operation='job', generation=lease['generation']))
        self.app.tick('site')
        self.assertEqual('bound', self.inspect(lease)['state'])
        self.call('provider.result', dict(provider='provider', operation='job', generation=lease['generation'], state='uncertain', result={}))
        self.now.return_value = 2000000
        self.app.tick('site')
        self.assertEqual('bound', self.inspect(lease)['state'])

    def test_finished_legacy_exchange_releases_without_phone_and_keeps_result(self):
        lease = self.acquire('legacy')
        self.bound(lease)
        self.call('cap.invoke', dict(lease=lease['id'], operation='job', inputs={'value': 'ciphertext'}))
        self.call('provider.claim', dict(provider='provider', operation='job', generation=lease['generation']))
        self.call('provider.result', dict(provider='provider', operation='job', generation=lease['generation'], state='succeeded', result={'value': 'encrypted reply'}))
        self.assertEqual('released', self.inspect(lease)['state'])
        self.assertEqual({'value': 'encrypted reply'}, self.call('cap.status', {'operation': 'job'})['result'])

    def test_non_opted_in_terminal_reservation_is_not_expired(self):
        self.offer({})
        lease = self.acquire('terminal-like')
        self.now.return_value = 1000000
        self.app.tick('site')
        self.assertEqual('fencing', self.inspect(lease)['state'])

    def test_existing_reservation_gets_migration_grace_after_restart(self):
        self.offer({})
        lease = self.acquire('old')
        self.offer(self.policy)
        self.app = Application(self.tmp.name)
        self.now.return_value = 5000
        self.app.tick('site')
        self.assertEqual('fencing', self.inspect(lease)['state'])
        self.now.return_value = 5121
        self.app.tick('site')
        self.assertEqual('released', self.inspect(lease)['state'])

    def test_invalid_policy_rejected_before_offer_changes(self):
        for idle in (-1, True, '120'):
            with self.assertRaises(Fault):
                self.offer({'idle_seconds': idle})


if __name__ == '__main__':
    unittest.main()
