"""Combined lifecycle keeps fence and replay guarantees through reconnects."""
import os
import tempfile
import unittest
from biomerte.app import Application, initialize
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
