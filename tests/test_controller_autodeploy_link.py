"""Paired link adoption must prove source and current authenticated metadata."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
probe = types.ModuleType('paired_controller_autodeploy_probe')
path = os.path.join(ROOT, 'deploy', 'controller_autodeploy_probe.py')
with open(path, 'rb') as stream:
    exec(compile(stream.read(), path, 'exec'), probe.__dict__)


class PairedSourceTests(unittest.TestCase):
    def test_old_configuration_keeps_existing_gateway_gate(self):
        with mock.patch.object(probe, 'gateway', return_value={'check': 'gateway_registration'}) as gateway:
            with mock.patch.object(probe, 'source') as source:
                self.assertEqual(probe.deployment_gateway({}), {'check': 'gateway_registration'})
        self.assertEqual(source.call_count, 0)
        gateway.assert_called_once_with({})

    def test_paired_gateway_rejects_missing_release_and_old_link_source(self):
        config = {'linked_services': [{'manager': 'launchd', 'label': 'controller-link'}]}
        with mock.patch.object(probe, 'gateway') as gateway:
            with self.assertRaises(ValueError):
                probe.deployment_gateway(config)
            with mock.patch.object(probe, 'source', side_effect=ValueError('controller_source_mismatch')):
                with self.assertRaises(ValueError):
                    probe.deployment_gateway(config, '/expected/release')
        self.assertEqual(gateway.call_count, 0)

    def test_paired_gateway_checks_the_selected_link_before_accepting_registration(self):
        service = {'manager': 'launchd', 'label': 'controller-link'}
        config = {'linked_services': [service]}
        proof = {'pid': 123, 'source': '/expected/release'}
        with mock.patch.object(probe, 'source', return_value=proof) as source:
            with mock.patch.object(probe, 'gateway', return_value={'node': 'mac'}):
                result = probe.deployment_gateway(config, '/expected/release')
        source.assert_called_once_with({'service': service}, '/expected/release')
        self.assertEqual(result, {'node': 'mac', 'linked_sources': [proof]})


class LinkPreflightTests(unittest.TestCase):
    def setUp(self):
        self.release = os.path.realpath(tempfile.mkdtemp(prefix='link-preflight-'))
        os.mkdir(os.path.join(self.release, 'deploy'))
        os.mkdir(os.path.join(self.release, 'biomerte'))
        for relative in ('biomerte/__init__.py', 'biomerte/network_addresses.py'):
            with open(os.path.join(self.release, relative), 'w') as stream:
                stream.write('')
        self.config = {'python': sys.executable, 'preflight_timeout': 30}
    def tearDown(self):
        shutil.rmtree(self.release)
    def test_old_release_without_gateway_entrypoint_is_rejected(self):
        with mock.patch.object(probe.subprocess, 'check_output') as command:
            with self.assertRaises(ValueError):
                probe.link_preflight(self.config, self.release)
        self.assertEqual(command.call_count, 0)
    def test_link_import_failure_is_rejected_before_activation(self):
        with open(os.path.join(self.release, 'deploy', 'gateway-agent.py'), 'w') as stream:
            stream.write("raise RuntimeError('candidate import failure')\n")
        with self.assertRaises(subprocess.CalledProcessError):
            probe.link_preflight(self.config, self.release)
    def test_staged_preflight_pins_source_without_inheriting_old_overlay(self):
        with open(os.path.join(self.release, 'deploy', 'gateway-agent.py'), 'w') as stream:
            stream.write('')
        with mock.patch.dict(os.environ, {'PYTHONPATH': '/old/pinned/release'}):
            with mock.patch.object(probe.subprocess, 'check_output', return_value=b'') as command:
                result = probe.link_preflight(self.config, self.release)
        args, kwargs = command.call_args
        self.assertEqual(args[0][0:3], [sys.executable, '-S', '-B'])
        self.assertEqual(args[0][-1], self.release)
        self.assertEqual(kwargs['cwd'], self.release)
        self.assertEqual(kwargs['env']['PYTHONPATH'], self.release)
        self.assertEqual(result, {'check': 'gateway_link_imports', 'source': self.release})


class NetworkGateTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='network-gate-')
        with open(os.path.join(self.home, 'node.json'), 'w') as stream:
            json.dump({'node': 'identity', 'site': 'site'}, stream)
        with open(os.path.join(self.home, 'bridge.key'), 'w') as stream:
            stream.write('fixture-key')
        self.config = {'home': self.home, 'gateway': {
            'origin': 'https://gateway.invalid', 'key_file': os.path.join(self.home, 'bridge.key'),
            'node': 'mac', 'require_network': True}}
        self.row = {'id': 'mac', 'host': os.uname().nodename, 'enabled': 1, 'ready': 1, 'seen': 990}
    def tearDown(self):
        shutil.rmtree(self.home)
    def check(self, network):
        row = dict(self.row)
        if network is not None:
            row['network'] = network
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {'site': 'site', 'nodes': [row]}).encode('utf-8')
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(probe, 'build_opener', return_value=opener):
            with mock.patch.object(probe.time, 'time', return_value=1000):
                return probe.gateway(self.config)
    def test_opt_in_rejects_old_gateway_and_stale_or_unavailable_reports(self):
        for network in (None, {}, {'self_report': {'current': False, 'status': 'stale', 'addresses': []}},
                        {'self_report': {'current': False, 'status': 'unavailable', 'addresses': []}}):
            with self.assertRaises(ValueError):
                self.check(network)
    def test_current_empty_snapshot_is_valid_and_does_not_reuse_old_addresses(self):
        result = self.check({'self_report': {'current': True, 'status': 'current', 'addresses': []}})
        self.assertTrue(result['network_report_current'])
    def test_old_gateway_remains_supported_when_gate_is_not_enabled(self):
        self.config['gateway'].pop('require_network')
        self.assertEqual(self.check(None)['node'], 'mac')


class PairedRecoveryTests(unittest.TestCase):
    def setUp(self):
        from tests import test_controller_autodeploy
        self.base = test_controller_autodeploy
        self.base.WatcherTests.setUp(self)
        commands = [['/fixture/controller', '{commit}'], ['/fixture/link', '{commit}']]
        self.config['restart_commands'] = commands
        self.worker.config['restart_commands'] = commands
        self.worker._restart = lambda release: self.base.watcher.Watcher._restart(self.worker, release)
    def tearDown(self):
        self.base.WatcherTests.tearDown(self)
    def test_failed_link_rolls_back_both_services_then_new_main_patch_recovers(self):
        calls = []
        def command(argv, *args, **kwargs):
            calls.append(tuple(argv))
            if argv == ['/fixture/link', self.base.B]:
                raise self.base.watcher.DeployError('command_exit_7')
            return b''
        with mock.patch.object(self.base.watcher, 'run_command', side_effect=command):
            self.worker.tick()
            self.worker.head = self.base.B
            self.worker.tick()
            self.assertEqual(self.worker.state['last_good']['commit'], self.base.A)
            self.assertEqual(self.worker.state['rejected_commit'], self.base.B)
            self.assertIsNone(self.worker.state['pending'])
            self.assertEqual(calls[-4:], [
                ('/fixture/controller', self.base.B), ('/fixture/link', self.base.B),
                ('/fixture/controller', self.base.A), ('/fixture/link', self.base.A)])
            resumed = self.base.Harness(self.config)
            resumed._restart = lambda release: self.base.watcher.Watcher._restart(resumed, release)
            resumed.head = self.base.C
            resumed.tick()
        self.assertEqual(resumed.state['last_good']['commit'], self.base.C)
        self.assertIsNone(resumed.state['pending'])
        self.assertEqual(calls[-2:], [
            ('/fixture/controller', self.base.C), ('/fixture/link', self.base.C)])


if __name__ == '__main__':
    unittest.main()
