"""Completion framing preserves legacy result semantics without size truncation."""
import sys
import json
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'deploy'))
import result_delivery as delivery


class ResultDeliveryTests(unittest.TestCase):
    def test_large_ascii_frames_round_trip_without_socket(self):
        result = {'content': [{'type': 'text', 'text': '\u2603\U0001f600"\\' * 90000}]}
        body = {'id': 'durable-job', 'lease': 'original-lease', 'result': result}
        calls = []

        def send(path, frame):
            self.assertLess(len(json.dumps(frame).encode('utf-8')), 500000)
            self.assertEqual(frame['id'], body['id'])
            self.assertEqual(frame['lease'], body['lease'])
            calls.append((path, frame))
            return {'accepted': True}

        class Socket:
            def complete(self, body):
                raise AssertionError('Large result must not depend on socket capacity')

        self.assertTrue(delivery.complete(body, send, Socket())['accepted'])
        chunks = [frame['data'] for path, frame in calls if path == 'result-chunk']
        self.assertEqual(json.loads(''.join(chunks)), result)
        self.assertEqual(calls[-1][1]['parts'], len(chunks))
        self.assertEqual(calls[-1][0], 'result-commit')

    def test_small_result_keeps_socket_and_http_compatibility(self):
        body = {'id': 'id', 'lease': 'lease', 'result': {'content': []}}
        calls = []
        self.assertEqual(delivery.complete(body, lambda path, value: calls.append((path, value))), None)
        self.assertEqual(calls, [('complete', body)])

    def test_stale_lease_stops_upload_before_commit(self):
        body = {'id': 'id', 'lease': 'lease', 'result': {'content': [{'text': 'x' * 200000}]}}
        calls = []
        def send(path, frame):
            calls.append(path)
            return {'accepted': False}
        self.assertEqual(delivery.complete(body, send), {'accepted': False})
        self.assertEqual(calls, ['result-chunk'])
