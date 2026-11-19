import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


def load(name):
    path = os.path.join(os.path.dirname(__file__), '..', 'deploy', name + '.py')
    spec = importlib.util.spec_from_file_location(name, path)
    if hasattr(importlib.util, 'module_from_spec'):
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    else:  # Python 3.4 on the Pi
        module = spec.loader.load_module(spec.name)
    return module


class ControllerAddressHeartbeatTests(unittest.TestCase):
    def report(self, address='192.168.1.55'):
        return {'version': 1, 'addresses': [
            {'address': address, 'family': 'ipv4', 'interface': 'en1',
             'scope': 'private', 'prefix_length': 24, 'network': '192.168.1.0/24'}]}

    def test_both_link_modes_refresh_network_snapshot_on_each_heartbeat(self):
        for name in ('local_link', 'ha_link'):
            module = load(name)
            health = {'realms': {'site': 'ready'}}
            with patch.object(module, 'network_registration',
                              side_effect=[self.report('192.168.1.23'),
                                           self.report('192.168.1.81')]):
                if name == 'local_link':
                    before = module.heartbeat_payload(health)
                    after = module.heartbeat_payload(health)
                else:
                    before = module.heartbeat_payload(health, 7)
                    after = module.heartbeat_payload(health, 7)
            self.assertEqual(before['network']['addresses'][0]['address'], '192.168.1.23')
            self.assertEqual(after['network']['addresses'][0]['address'], '192.168.1.81')
            self.assertTrue(after['ready'])
            self.assertNotIn('controller', after)
            self.assertNotIn('provider', after)
            self.assertNotIn('timestamp', after['network'])

    def test_local_health_failure_still_reports_addresses_but_cannot_accept_work(self):
        module = load('local_link')
        sent = []
        calls = []
        def fetch(url, **kwargs):
            calls.append((url, kwargs))
            raise OSError('controller unavailable')
        def send(path, body):
            sent.append((path, body))
            return {}
        with patch.object(module, 'network_registration', return_value=self.report()):
            self.assertFalse(module.heartbeat_once('http://127.0.0.1:8740', fetch, send))
        self.assertEqual(calls, [('http://127.0.0.1:8740/health', {'timeout': 3})])
        self.assertEqual(sent[0][0], 'ha/heartbeat')
        self.assertFalse(sent[0][1]['ready'])
        self.assertEqual(sent[0][1]['network'], self.report())

    def test_collection_failure_does_not_change_ready_controller_admission(self):
        module = load('local_link')
        sent = []
        with patch.object(module, 'network_registration', return_value=None):
            ready = module.heartbeat_once('http://127.0.0.1:8740',
                lambda *a, **kw: {'realms': {'site': 'ready'}},
                lambda path, body: sent.append(body))
        self.assertTrue(ready)
        self.assertIsNone(sent[0]['network'])
        self.assertTrue(sent[0]['ready'])

    def test_gateway_rejection_does_not_report_a_successful_heartbeat(self):
        module = load('local_link')
        def send(path, body):
            raise OSError('gateway unavailable')
        with patch.object(module, 'network_registration', return_value=self.report()):
            with self.assertRaises(OSError):
                module.heartbeat_once('http://127.0.0.1:8740',
                    lambda *a, **kw: {'realms': {'site': 'ready'}}, send)

    def test_ha_health_failure_uses_existing_authenticated_registration(self):
        module = load('ha_link')
        targets = []
        calls = []
        class Stop(Exception):
            pass
        class Thread:
            def __init__(self, target, **kwargs):
                targets.append(target)
            def start(self):
                pass
        def sleep(seconds):
            raise Stop()
        def fetch(url, body=None, token=None, **kwargs):
            calls.append((url, body, token, kwargs))
            if url.endswith('/health'):
                raise OSError('controller unavailable')
            return {'primary': None}
        def dispatch_loop(*args):
            targets[0]()
        connection = types.SimpleNamespace(
            execute=lambda *a: types.SimpleNamespace(fetchone=lambda: (7,)),
            close=lambda: None)
        mocks = {
            'persistent_link': types.SimpleNamespace(ControllerLink=lambda *a: object()),
            'result_delivery': types.SimpleNamespace(complete=lambda *a: None),
            'controller_inbox': types.SimpleNamespace(dispatch_loop=dispatch_loop)}
        with tempfile.TemporaryDirectory() as home:
            with open(os.path.join(home, 'node.json'), 'w') as handle:
                json.dump({'node': 'app', 'site': 'site',
                           'gateway_journal': {'controller_id': 'pi'}}, handle)
            args = types.SimpleNamespace(home=home, port=8740, key=home + '/bridge.key')
            with patch.dict(sys.modules, mocks), patch.object(module.threading, 'Thread', Thread), \
                 patch.object(module.time, 'sleep', sleep), \
                 patch.object(module.sqlite3, 'connect', return_value=connection), \
                 patch.object(module, 'network_registration', return_value=self.report()):
                with self.assertRaises(Stop):
                    module.run(args, 'https://gateway.invalid', 'existing-token',
                               'existing-site-access', fetch, None, None, None)
        self.assertEqual(len(calls), 2)
        url, body, token, kwargs = calls[1]
        self.assertEqual(url, 'https://gateway.invalid/bridge/ha/heartbeat')
        self.assertEqual(token, 'existing-token')
        self.assertEqual(kwargs['site_access'], 'existing-site-access')
        self.assertEqual(body['position'], 7)
        self.assertFalse(body['ready'])
        self.assertEqual(body['network'], self.report())


if __name__ == '__main__':
    unittest.main()
