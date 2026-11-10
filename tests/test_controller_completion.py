"""HTTP claims cannot be acknowledged by an unrelated gateway socket."""
import json
import os
import queue
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'deploy'))
import persistent_link as link
import result_delivery as delivery


class Socket:
    def __init__(self, client, accepted=None, fail=False, legacy=False):
        self.client, self.accepted, self.fail = client, accepted, fail
        self.legacy = legacy
        self.sent = []

    def send(self, raw):
        if self.fail:
            raise OSError('Disconnected')
        frame = json.loads(raw)
        self.sent.append(frame)
        if self.accepted is not None:
            ack = {'type': 'accepted', 'id': frame['id'], 'accepted': self.accepted}
            if not self.legacy:
                ack['lease'] = frame['lease']
            self.client._receive(self, ack)


def client():
    value = link.ControllerLink.__new__(link.ControllerLink)
    value.jobs = queue.Queue()
    value.send_lock = threading.Lock()
    value.lock = threading.Lock()
    value.pending = {}
    value.socket = Socket(value)
    return value


class ControllerCompletionTests(unittest.TestCase):
    def setUp(self):
        self.client = client()
        self.body = {'id': 'same-job', 'lease': 'same-lease', 'result': {'content': []}}
        self.http = []

    def send(self, path, body):
        self.http.append((path, body))
        return {'accepted': True}

    def test_http_claim_with_live_socket_completes_without_ack_wait(self):
        started = time.monotonic()
        with patch.object(link.threading.Event, 'wait', side_effect=AssertionError('No socket owns this claim')):
            value = delivery.complete(self.body, self.send, self.client)
        self.assertEqual(value, {'accepted': True})
        self.assertLess(time.monotonic() - started, .5)
        self.assertEqual(self.client.socket.sent, [])
        self.assertEqual(self.http, [('complete', self.body)])

    def test_socket_delivery_retains_exact_session_and_uses_its_ack(self):
        ws = self.client.socket
        ws.accepted = True
        request = {'name': 'biome_native_call', 'arguments': {'body': 'unchanged native bytes'}}
        self.client._receive(ws, dict(type='job', id='same-job', lease='same-lease', request=request))
        job = self.client.next()
        self.assertIs(job['_delivery_socket'], ws)
        self.assertIs(job['request'], request)
        value = delivery.complete(self.body, self.send, self.client, job['_delivery_socket'])
        self.assertEqual(value, {'accepted': True})
        self.assertEqual(self.http, [])
        self.assertEqual(ws.sent, [dict(self.body, type='complete')])
        self.assertEqual(self.client.pending, {})

    def test_reconnected_socket_uses_http_without_ack_wait(self):
        old = self.client.socket
        self.client.socket = Socket(self.client)
        with patch.object(link.threading.Event, 'wait', side_effect=AssertionError('New socket did not deliver job')):
            self.assertTrue(delivery.complete(self.body, self.send, self.client, old)['accepted'])
        self.assertEqual(old.sent, [])
        self.assertEqual(self.client.socket.sent, [])
        self.assertEqual(self.http, [('complete', self.body)])
        self.assertEqual(self.client.pending, {})

    def test_socket_send_failure_falls_back_with_same_identity(self):
        ws = self.client.socket
        ws.fail = True
        self.assertTrue(delivery.complete(self.body, self.send, self.client, ws)['accepted'])
        self.assertEqual(self.http, [('complete', self.body)])
        self.assertEqual(self.client.pending, {})

    def test_missing_socket_ack_falls_back_with_same_identity(self):
        ws = self.client.socket
        with patch.object(link.threading.Event, 'wait', return_value=False) as wait:
            self.assertTrue(delivery.complete(self.body, self.send, self.client, ws)['accepted'])
        wait.assert_called_once_with(15)
        self.assertEqual(ws.sent, [dict(self.body, type='complete')])
        self.assertEqual(self.http, [('complete', self.body)])
        self.assertEqual(self.client.pending, {})

    def test_explicit_rejection_does_not_resubmit_stale_lease(self):
        ws = self.client.socket
        ws.accepted = False
        self.assertEqual(delivery.complete(self.body, self.send, self.client, ws), {'accepted': False})
        self.assertEqual(self.http, [])

    def test_ack_from_another_session_cannot_release_waiter(self):
        ws = self.client.socket
        item = {'ready': threading.Event(), 'socket': ws, 'lease': self.body['lease']}
        self.client.pending[self.body['id']] = item
        self.client._receive(Socket(self.client), {'type': 'accepted', 'id': self.body['id'], 'accepted': True})
        self.assertFalse(item['ready'].is_set())
        self.assertNotIn('accepted', item)

    def test_overlapping_job_lease_does_not_replace_existing_waiter(self):
        ws = self.client.socket
        old = {'ready': threading.Event(), 'socket': ws, 'lease': 'earlier-lease'}
        self.client.pending[self.body['id']] = old
        with patch.object(link.threading.Event, 'wait', side_effect=AssertionError('Existing ID has a waiter')):
            self.assertTrue(delivery.complete(self.body, self.send, self.client, ws)['accepted'])
        self.assertIs(self.client.pending[self.body['id']], old)
        self.assertEqual(ws.sent, [])
        self.assertEqual(self.http, [('complete', self.body)])

    def test_late_old_lease_ack_cannot_satisfy_new_waiter(self):
        ws = self.client.socket
        item = {'ready': threading.Event(), 'socket': ws, 'lease': self.body['lease']}
        self.client.pending[self.body['id']] = item
        self.client._receive(ws, {'type': 'accepted', 'id': self.body['id'],
                                  'lease': 'old-expired-lease', 'accepted': True})
        self.assertFalse(item['ready'].is_set())
        self.assertNotIn('accepted', item)
        self.client._receive(ws, {'type': 'accepted', 'id': self.body['id'],
                                  'lease': self.body['lease'], 'accepted': True})
        self.assertTrue(item['ready'].is_set())
        self.assertTrue(item['accepted'])

    def test_legacy_id_only_ack_immediately_reconciles_through_http(self):
        ws = self.client.socket
        ws.accepted = True
        ws.legacy = True
        started = time.monotonic()
        self.assertTrue(delivery.complete(self.body, self.send, self.client, ws)['accepted'])
        self.assertLess(time.monotonic() - started, .5)
        self.assertEqual(ws.sent, [dict(self.body, type='complete')])
        self.assertEqual(self.http, [('complete', self.body)])
        self.assertEqual(self.client.pending, {})


if __name__ == '__main__':
    unittest.main()
