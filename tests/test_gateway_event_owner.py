import json
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


class GatewayEventOwnerTests(unittest.TestCase):
    def test_event_store_receives_actual_application_identity(self):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        path = os.path.join(root, 'deploy', 'gateway-agent.py')
        module = types.ModuleType('gateway_event_owner_test')
        module.__file__ = path
        with patch.dict(sys.modules, {'mcp_events': types.SimpleNamespace(EventStore=None, CallbackError=ValueError),
                                     'result_delivery': types.SimpleNamespace(complete=None)}):
            with open(path, 'rb') as handle:
                exec(compile(handle.read(), path, 'exec'), module.__dict__)
        with tempfile.TemporaryDirectory() as home:
            with open(os.path.join(home, 'node.json'), 'w') as handle:
                json.dump({'node': 'a' * 64, 'admin_token': 'fixture-only'}, handle)
            with patch.object(module, 'EventStore') as store, patch.object(module, 'fetch') as fetch:
                module.event_service(os.path.join(home, 'events.sqlite'), 'http://local', home,
                                     'https://gateway.invalid', 'fixture-token', None)
                self.assertEqual(store.call_args[1]['controller'], 'a' * 64)
                fetch.assert_not_called()

    def test_missing_application_identity_does_not_create_unbound_store(self):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        path = os.path.join(root, 'deploy', 'gateway-agent.py')
        module = types.ModuleType('gateway_event_owner_missing_test')
        module.__file__ = path
        with patch.dict(sys.modules, {'mcp_events': types.SimpleNamespace(EventStore=None, CallbackError=ValueError),
                                     'result_delivery': types.SimpleNamespace(complete=None)}):
            with open(path, 'rb') as handle:
                exec(compile(handle.read(), path, 'exec'), module.__dict__)
        with tempfile.TemporaryDirectory() as home:
            with open(os.path.join(home, 'node.json'), 'w') as handle:
                json.dump({}, handle)
            with patch.object(module, 'EventStore') as store:
                with self.assertRaises(KeyError):
                    module.event_service('unused', 'http://local', home, 'https://gateway.invalid', 'fixture', None)
                store.assert_not_called()
