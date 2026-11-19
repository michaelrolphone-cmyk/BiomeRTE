"""Prevent a default controller or stale registration satisfying readiness."""
import json
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
probe = types.ModuleType('controller_autodeploy_probe')
path = os.path.join(ROOT, 'deploy', 'controller_autodeploy_probe.py')
with open(path, 'rb') as stream:
    exec(compile(stream.read(), path, 'exec'), probe.__dict__)

class Response:
    def __init__(self, value):
        self.value = value
    def __enter__(self):
        return self
    def __exit__(self, *unused):
        return False
    def read(self):
        return json.dumps(self.value).encode('utf-8')

class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='autodeploy-probe-')
        for name, value in [('node.json', {'node': 'identity', 'site': 'site'}), ('key', None)]:
            with open(os.path.join(self.directory, name), 'w') as stream:
                stream.write('fixture-key' if value is None else json.dumps(value))
        self.config = {'home': self.directory, 'gateway': {
            'origin': 'https://gateway.invalid', 'key_file': os.path.join(self.directory, 'key'),
            'node': 'selected', 'freshness_seconds': 60}}
        self.row = {'id': 'selected', 'host': os.uname().nodename, 'enabled': 1, 'ready': 1, 'seen': 990}
    def tearDown(self):
        shutil.rmtree(self.directory)
    def invoke(self, rows):
        opener = mock.Mock()
        opener.open.return_value = Response({'site': 'site', 'nodes': rows})
        with mock.patch.object(probe, 'build_opener', return_value=opener):
            with mock.patch.object(probe.time, 'time', return_value=1000):
                return probe.gateway(self.config)
    def test_accepts_own_fresh_authenticated_registration(self):
        self.assertEqual(self.invoke([self.row])['node'], 'selected')
    def test_default_other_controller_is_not_selected_controller(self):
        other = dict(self.row, id='default-other')
        with self.assertRaises(ValueError):
            self.invoke([other])
    def test_stale_own_registration_is_not_ready(self):
        with self.assertRaises(ValueError):
            self.invoke([dict(self.row, seen=900)])
    def test_wrong_execution_host_is_not_ready(self):
        with self.assertRaises(ValueError):
            self.invoke([dict(self.row, host='another-host')])
    def test_duplicate_identity_is_rejected(self):
        with self.assertRaises(ValueError):
            self.invoke([self.row, dict(self.row)])
    def test_plain_http_cannot_receive_credential(self):
        self.config['gateway']['origin'] = 'http://gateway.invalid'
        with mock.patch.object(probe, 'build_opener') as request:
            with self.assertRaises(ValueError):
                probe.gateway(self.config)
        self.assertEqual(request.call_count, 0)

if __name__ == '__main__':
    unittest.main()
