"""Combined lifecycle keeps fence and replay guarantees through reconnects."""
import os
import tempfile
import unittest
from biomerte.app import Application, initialize
from biomerte.api import Context
from biomerte import providers
from biomerte.store import Fault, uid

class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        initialize(self.tmp.name)
        self.app = Application(self.tmp.name)
        self.call('cap.define', dict(name='test.transport', inputs={'value':'str'}, outputs={'value':'str'}))
        self.call('cap.offer', dict(provider='test-provider', contract='test.transport:1', resource='test-slot', capacity=1))

    def call(self, op, args, rid=None):
        return self.app.call(op, args, rid or uid())

    def submit(self, operation, value='encrypted'):
        return self.call('cap.submit', dict(contract='test.transport:1', resource='test-slot',
            operation=operation, inputs={'value':value}))

    def complete(self, action):
        self.call('cap.fenced', dict(provider='test-provider', lease=action['lease'], generation=action['generation']))
        self.call('provider.claim', dict(provider='test-provider', operation=action['id'], generation=action['generation']))
        return self.call('provider.result', dict(provider='test-provider', operation=action['id'],
            generation=action['generation'], state='succeeded', result={'value':'ciphertext'}))

    def test_submission_cannot_execute_before_fence_and_releases_after_result(self):
        action = self.submit('one')
        self.assertEqual([], self.call('provider.poll', {'provider':'test-provider'})['actions'])
        with self.assertRaises(Fault):
            self.call('provider.claim',dict(provider='test-provider',operation='one',generation=action['generation']))
        self.complete(action)
        self.assertEqual('released', self.call('cap.lease', {'lease':action['lease']})['state'])
        result = self.submit('one')
        self.assertEqual('succeeded', result['state'])
        self.assertEqual(action['generation'], result['generation'])
        self.assertEqual([], self.call('provider.poll', {'provider':'test-provider'})['actions'])

    def test_queued_submission_survives_restart_and_promotes_without_client(self):
        first = self.submit('first')
        second = self.submit('second')
        self.assertEqual('queued', second['state'])
        self.app = Application(self.tmp.name)
        self.complete(first)
        for unused in range(3):
            self.app.tick('site')
        second = self.call('cap.submission', {'operation':'second'})
        self.assertIn('generation', second)
        self.complete(second)
        self.assertEqual('succeeded', self.call('cap.submission', {'operation':'second'})['state'])

    def test_normal_status_preserves_accepted_queue_identity_before_dispatch(self):
        first = self.submit('occupies-resource')
        queued = self.submit('waiting-for-resource')
        self.assertEqual('queued', queued['state'])
        self.app = Application(self.tmp.name)
        self.assertEqual(queued, self.call('cap.status', {'operation': queued['id']}))
        self.assertIsNone(self.app.stores['site'].execute(
            lambda tx: tx.get('actions', queued['id']), read_only=True))
        self.complete(first)
        self.app.tick('site')
        action = self.call('cap.status', {'operation': queued['id']})
        self.assertEqual(queued['lease'], action['lease'])
        self.complete(action)
        self.assertEqual('succeeded', self.call('cap.status', {'operation': queued['id']})['state'])

    def test_pre_dispatch_status_checks_owner_and_unknown_or_recovery_semantics(self):
        self.submit('occupies-resource')
        queued = self.submit('known-queued')
        other = Context(self.app, actor='different-caller', permissions={'cap.status'})
        with self.assertRaises(Fault) as denied:
            self.app.stores['site'].execute(
                lambda tx: providers.status(tx, other, queued['id']), read_only=True)
        self.assertEqual(403, denied.exception.status)
        with self.assertRaises(Fault) as missing:
            self.call('cap.status', {'operation': 'not-submitted'})
        self.assertEqual(404, missing.exception.status)
        self.assertFalse(self.call('cap.status', {'operation': 'not-submitted', 'recovery': True})['replay_allowed'])

    def test_missing_original_lease_and_action_never_replay_the_submission(self):
        first = self.submit('interrupted')
        def retained_submission_only(tx):
            tx.drop('actions', first['id'])
            tx.drop('guards', first['guard'])
            tx.drop('leases', first['lease'])
            tx.drop('_submission_leases', first['lease'])
        self.app.stores['site'].execute(retained_submission_only)
        self.app = Application(self.tmp.name)
        self.app.tick('site')
        result = self.submit('interrupted')
        self.assertEqual('missing_unknown', result['state'])
        self.assertEqual(first['lease'], result['lease'])
        self.assertIs(result['replay_allowed'], False)
        self.assertEqual(result, self.call('cap.submission', {'operation': 'interrupted'}))
        self.assertFalse(self.call('provider.poll', {'provider': 'test-provider'})['fences'])
        self.assertIsNone(self.app.stores['site'].execute(
            lambda tx: tx.get('_submission_active', first['id']), read_only=True))
        with self.assertRaises(Fault):
            self.submit('interrupted', 'different')

    def test_conflicting_retry_never_replaces_encrypted_input(self):
        self.submit('same')
        with self.assertRaises(Fault):
            self.submit('same','different')

    def test_uncertain_result_keeps_fence_for_reconciliation(self):
        action=self.submit('uncertain')
        self.call('cap.fenced', dict(provider='test-provider',lease=action['lease'],generation=action['generation']))
        self.call('provider.claim',dict(provider='test-provider',operation=action['id'],generation=action['generation']))
        self.call('provider.result',dict(provider='test-provider',operation=action['id'],generation=action['generation'],state='uncertain',result={}))
        self.app.tick('site')
        self.assertEqual('bound',self.call('cap.lease',{'lease':action['lease']})['state'])
