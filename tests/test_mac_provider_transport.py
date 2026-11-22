import importlib.util
from pathlib import Path
import unittest
from unittest import mock


class MacProviderTransport(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'providers/mac-terminal/mac_provider.py'
        spec = importlib.util.spec_from_file_location('mac_provider_for_routing', path)
        self.provider = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.provider)

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
