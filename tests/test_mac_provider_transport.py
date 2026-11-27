import importlib.util
import json
from pathlib import Path
from queue import Queue
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest import mock


class MacProviderTransport(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1] / 'providers/mac-terminal'
        path = source / 'mac_provider.py'
        spec = importlib.util.spec_from_file_location('mac_provider_for_routing', path)
        self.provider = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.provider)
        spec = importlib.util.spec_from_file_location('persistent_transport_for_routing',
                                                      source / 'persistent_transport.py')
        self.transport = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.transport)

    @contextmanager
    def connected(self):
        socket = TestSocket()
        client = self.transport.PersistentTransport('https://example.invalid', lambda: {})
        client.reply_timeout = .05
        client.socket = socket
        client._connect = lambda: socket
        receiver = threading.Thread(target=client._receive, args=(socket,), daemon=True)
        receiver.start()
        with ThreadPoolExecutor(max_workers=2) as pool:
            try:
                yield client, socket, pool
            finally:
                client._disconnect()
                receiver.join(1)

    def test_pong_without_invocation_reply_falls_back_with_original_mutation_id(self):
        provider = self.provider
        expected = {'accepted': True}
        http_reply = {'result': {'structuredContent': expected}}
        with mock.patch.object(provider, 'credentials', return_value={'access_token': 'fixture'}), \
             mock.patch.object(provider, 'request', return_value=http_reply) as http, \
             self.connected() as (client, socket, pool), mock.patch.object(provider, '_persistent', client):
            future = pool.submit(provider.invoke, 'cap.invoke', {'operation': 'already-submitted'}, 'same-id')
            frame = socket.sent.get(timeout=1)
            socket.frames.put(json.dumps({'type': 'pong'}))
            self.assertEqual(expected, future.result(timeout=2))
            sent = http.call_args.args[1]
            self.assertEqual('same-id', sent['id'])
            self.assertEqual(frame['arguments'], sent['params']['arguments'])
            self.assertFalse(socket.closed)
            self.assertEqual({}, client.pending)

    def test_idle_poll_falls_back_to_a_real_read_and_retires_socket(self):
        provider = self.provider
        expected = {'actions': [], 'fences': [{'id': 'active-grant'}]}
        with mock.patch.object(provider, 'credentials', return_value={'access_token': 'fixture'}), \
             mock.patch.object(provider, 'request', return_value={'result': {'structuredContent': expected}}) as http, \
             self.connected() as (client, socket, pool), mock.patch.object(provider, '_persistent', client):
            future = pool.submit(provider.invoke, 'provider.poll', {'provider': 'worker'}, 'same-poll')
            frame = socket.sent.get(timeout=1)
            self.assertEqual(expected, future.result(timeout=2))
            self.assertEqual(frame['arguments'], http.call_args.args[1]['params']['arguments'])
            self.assertTrue(socket.closed)
            self.assertIsNone(client.socket)

    def test_timed_out_poll_does_not_interrupt_another_invocation(self):
        expires = threading.Event()
        poll_waiting = threading.Event()

        class PollReply(threading.Event):
            def wait(self, timeout=None):
                poll_waiting.set()
                expires.wait(1)
                return self.is_set()

        with self.connected() as (client, socket, pool):
            client.reply_timeout = 2
            events = iter((PollReply(), threading.Event()))
            with mock.patch.object(self.transport, 'threading', SimpleNamespace(Event=lambda: next(events))):
                poll = pool.submit(client.invoke, {'operation': 'provider.poll'})
                socket.sent.get(timeout=1)
                self.assertTrue(poll_waiting.wait(1))
                other = pool.submit(client.invoke, {'operation': 'cap.status'})
                reply_to = socket.sent.get(timeout=1)['id']
                expires.set()
                with self.assertRaises(ConnectionError):
                    poll.result(timeout=1)
                self.assertFalse(socket.closed)
                with self.assertRaises(ConnectionError):
                    client.invoke({'operation': 'cap.status'})
                socket.frames.put(json.dumps({'type': 'reply', 'id': reply_to, 'result': {'available': True}}))
                self.assertEqual({'result': {'available': True}}, other.result(timeout=1))
                self.assertTrue(socket.closed)

    def test_enrolled_provider_identity_reaches_websocket_and_http_after_disconnect(self):
        provider = self.provider
        reply = {'result': {'structuredContent': None, 'content': [{'type': 'text', 'text': '{"actions":[]}'}]}}
        envelope = {'operation': 'provider.poll', 'arguments': {'provider': 'a' * 64},
                    'request_id': 'same-poll', 'realm': 'site',
                    'provider_context': {'provider': 'a' * 64, 'host': 'enrolled-host'}}
        remote = provider.Remote.__new__(provider.Remote)
        remote.node = 'a' * 64
        remote.last_health = float('inf')
        with mock.patch.object(provider, '_persistent') as ws, \
             mock.patch.object(provider, 'credentials', return_value={'access_token': 'fixture'}), \
             mock.patch.object(provider.socket, 'gethostname', return_value='enrolled-host'), \
             mock.patch.object(provider, 'request', return_value=reply) as http:
            ws.invoke.side_effect = ConnectionError('detached')
            self.assertEqual(remote.call('provider.poll', {'provider': remote.node}, 'same-poll'), {'actions': []})
            ws.invoke.assert_called_once_with(envelope)
            sent = http.call_args.args[1]
            self.assertEqual(sent['id'], 'same-poll')
            self.assertEqual(sent['params']['arguments'], envelope)

    def test_vision_provider_call_with_explicit_enrollment(self):
        with mock.patch.object(self.provider, 'invoke') as invoke:
            self.provider.invoke_provider('cap.status', {'operation': 'old-id'}, 'read-id', 'site', 'b' * 64)
            self.assertEqual(invoke.call_args.kwargs['provider_context']['provider'], 'b' * 64)
        with self.assertRaises(ValueError):
            self.provider.invoke_provider('provider.poll', {}, 'request')

    def test_attachment_error_carries_public_status_without_joining_the_http_retry_loop(self):
        provider = self.provider
        for status in (403, 429, 503):
            reply = {'result': {'isError': True, 'structuredContent': {'status': status},
                                'content': [{'type': 'text', 'text': 'Managed stream unavailable'}]}}
            with self.subTest(status=status), mock.patch.object(provider, '_persistent') as ws, \
                 mock.patch.object(provider, 'credentials', return_value={'access_token': 'fixture'}):
                ws.invoke.return_value = reply
                with self.assertRaisesRegex(RuntimeError, 'Managed stream unavailable') as caught:
                    provider.invoke_provider('stream.attach', {'session': 'original'}, 'original-request',
                                             provider='b' * 64)
                self.assertEqual(getattr(caught.exception, 'status', None), status)
                ws.invoke.assert_called_once()


class TestSocket:
    def __init__(self):
        self.sent = Queue()
        self.frames = Queue()
        self.closed = False

    def send(self, message):
        self.sent.put(json.loads(message))

    def recv(self):
        return self.frames.get()

    def close(self):
        self.closed = True
        self.frames.put(None)
