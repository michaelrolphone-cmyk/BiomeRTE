"""Transport reply liveness must not become a capability/job deadline."""
import importlib.util
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / 'providers' / 'mac-terminal'

def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, str(SOURCE / filename))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value

transport = module('transport_liveness_test', 'persistent_transport.py')

class Socket:
    def __init__(self):
        self.sent = []
        self.closed = False
    def send(self, message):
        self.sent.append(message)
    def close(self):
        self.closed = True

class PersistentReplyLiveness(unittest.TestCase):
    def test_missing_reply_returns_to_durable_fallback_without_disconnect(self):
        client = transport.PersistentTransport('https://example.invalid', lambda: {})
        client.reply_timeout = .03
        socket = Socket()
        client.socket = socket
        client._connect = lambda: socket
        finished = threading.Event()
        errors = []
        def invoke():
            try:
                client.invoke({'request_id': 'same-durable-request', 'operation': 'cap.status'})
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()
        thread = threading.Thread(target=invoke, daemon=True)
        thread.start()
        try:
            self.assertTrue(finished.wait(.5), 'A lost RPC reply stranded a live connection')
            self.assertIsInstance(errors[0], ConnectionError)
            self.assertFalse(socket.closed, 'One lost reply interrupted unrelated requests')
            self.assertEqual(client.pending, {})
        finally:
            client._disconnect()
            thread.join(1)

    def test_other_request_finishes_while_one_reply_is_missing(self):
        client = transport.PersistentTransport('https://example.invalid', lambda: {})
        client.reply_timeout = .08
        socket = Socket()
        client.socket = socket
        client._connect = lambda: socket
        completed = []
        def invoke(identifier):
            try:
                completed.append((identifier, client.invoke({'request_id': identifier})))
            except ConnectionError:
                completed.append((identifier, 'pending'))
        first = threading.Thread(target=invoke, args=('slow',), daemon=True)
        second = threading.Thread(target=invoke, args=('fast',), daemon=True)
        first.start()
        second.start()
        try:
            deadline = time.monotonic() + 1
            while len(socket.sent) < 2 and time.monotonic() < deadline:
                time.sleep(.001)
            import json
            fast = next(json.loads(s)['id'] for s in socket.sent if json.loads(s)['arguments']['request_id'] == 'fast')
            with client.lock:
                client.pending[fast]['result'] = {'value': 'ok'}
                client.pending[fast]['ready'].set()
            second.join(.5)
            first.join(.5)
            self.assertFalse(first.is_alive())
            self.assertIn(('fast', {'result': {'value': 'ok'}}), completed)
            self.assertIn(('slow', 'pending'), completed)
            self.assertFalse(socket.closed)
        finally:
            client._disconnect()
            first.join(1)
            second.join(1)

    def test_http_fallback_preserves_original_mutation_request_id(self):
        provider = module('mac_transport_fallback_test', 'mac_provider.py')
        class NoReply:
            socket = object()
            def invoke(self, arguments):
                persistent.append(arguments)
                raise ConnectionError('Reply not received')
        persistent, http = [], []
        provider._persistent = NoReply()
        provider._persistent_after = 0
        def request(url, rpc, headers):
            http.append(rpc['params']['arguments'])
            return {'result': {'structuredContent': {'accepted': True}}}
        with mock.patch.object(provider, 'credentials', return_value={'access_token': 'test-only'}), mock.patch.object(provider, 'request', side_effect=request):
            value = provider.invoke('cap.invoke', {'operation': 'existing-command'}, 'unchanged-request')
        self.assertEqual(value, {'accepted': True})
        self.assertEqual(persistent, http)
        self.assertEqual(http[0]['request_id'], 'unchanged-request')
        self.assertEqual(http[0]['arguments']['operation'], 'existing-command')
