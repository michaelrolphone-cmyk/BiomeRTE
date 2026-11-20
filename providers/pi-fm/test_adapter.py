import json
import os
import tempfile
import unittest
import server

class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.calls = []
        self.original = server.radio.handle
        def handle(method, inputs, context):
            self.calls.append(dict(inputs))
            if inputs.get('action') == 'broken':
                raise ValueError('test failure')
            return {'action': inputs.get('action'), 'job_id': 'test-job'}
        server.radio.handle = handle
        self.dispatch = server.Dispatcher(self.home.name, None)
    def tearDown(self):
        self.dispatch.db.close()
        server.radio.handle = self.original
        self.home.cleanup()
    def test_transmit_replay_and_restart(self):
        request = {'operation': 'pulse-1', 'inputs': {'action': 'transmit'}}
        first = self.dispatch.execute(request)
        self.assertTrue(first['ok'])
        self.assertEqual(len(self.calls[0]['request_id']), 64)
        self.assertEqual(first, self.dispatch.execute(request))
        self.dispatch.db.close()
        self.dispatch = server.Dispatcher(self.home.name, None)
        self.assertEqual(first, self.dispatch.execute(request))
        self.assertEqual(len(self.calls), 1)
    def test_conflicting_replay_does_not_execute(self):
        self.dispatch.execute({'operation': 'x', 'inputs': {'action': 'status'}})
        result = self.dispatch.execute({'operation': 'x', 'inputs': {'action': 'stop'}})
        self.assertFalse(result['ok'])
        self.assertEqual(len(self.calls), 1)
    def test_failures_are_receipted(self):
        request = {'operation': 'e', 'inputs': {'action': 'broken'}}
        self.assertFalse(self.dispatch.execute(request)['ok'])
        self.dispatch.execute(request)
        self.assertEqual(len(self.calls), 1)
    def test_status_stop_capabilities_and_explicit_request_id(self):
        for action in ('status', 'stop', 'capabilities'):
            self.assertTrue(self.dispatch.execute({'operation': action, 'inputs': {'action': action}})['ok'])
        self.dispatch.execute({'operation': 't', 'inputs': {'action': 'transmit', 'request_id': 'explicit'}})
        self.assertEqual(self.calls[-1]['request_id'], 'explicit')

if __name__ == '__main__': unittest.main()
