"""Custody recovery boundaries, using disposable homes and no real services."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_controller_autodeploy import (
    ROOT, A, B, Harness, load_module, save, watcher)

custody = __import__('controller_custody')
launcher = load_module('custody_launcher',
    os.path.join(ROOT, 'deploy', 'controller_autodeploy_exec.py'))
service = load_module('custody_service',
    os.path.join(ROOT, 'deploy', 'controller_autodeploy_service.py'))
watchdog = load_module('custody_watchdog',
    os.path.join(ROOT, 'deploy', 'controller_watchdog.py'))


class CustodyRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = os.path.realpath(tempfile.mkdtemp(prefix='custody-recovery-'))
        self.addCleanup(shutil.rmtree, self.temporary)
        self.home = os.path.join(self.temporary, 'home')
        self.root = os.path.join(self.temporary, 'updater')
        self.old = os.path.join(self.temporary, 'old')
        self.new = os.path.join(self.temporary, 'new')
        os.makedirs(self.home)
        os.makedirs(self.root)
        self.node = {'node': 'source-node', 'site': 'site', 'admin_token': 'fixture'}
        save(os.path.join(self.home, 'node.json'), self.node)
        self.marker = {'format': 1, 'mode': 'frozen-source',
            'source_node': self.node['node'], 'site': self.node['site'],
            'service_id': 'logical-controller', 'authority_id': 'logical-node',
            'controller_aliases': ['source-node', 'other-source'],
            'migration_id': 'fixture-migration', 'source_digest': 'a' * 64}
        self.marker_path = os.path.join(self.home, 'controller-authority.json')
        self.make_release(self.old, False)
        self.make_release(self.new, True)
        self.config = {'root': self.root, 'home': self.home,
            'repository': '/fixture/repository', 'python': sys.executable,
            'controller_argv': [sys.executable, '-S', '-m', 'biomerte',
                                '--home', self.home, 'serve'],
            'restart_commands': [['/fixture/restart']],
            'startup_timeout': .01, 'probe_interval': .001}
        self.worker = Harness(self.config)
        self.worker.initialize(self.old)

    def make_release(self, path, compatible):
        for relative in ('biomerte/__init__.py', 'biomerte/app.py',
                         'biomerte/store.py', 'deploy/gateway-agent.py',
                         'deploy/mcp_events.py'):
            destination = os.path.join(path, relative)
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            with open(destination, 'w') as stream:
                stream.write('CUSTODY_PROTOCOL = 1\n' if compatible else '# legacy\n')

    def freeze(self):
        save(self.marker_path, self.marker)

    def select(self, path):
        watcher.atomic_link(self.worker.current_link, path)

    def pending(self):
        previous = self.worker.state['last_good']
        candidate = {'path': self.new, 'commit': A}
        self.select(self.new)
        self.worker._save(phase='verifying', current=candidate,
            pending={'candidate': candidate, 'previous': previous})
        return candidate

    def source_status(self):
        return {'mode': 'managed-source', 'node': self.node['node'],
            'automatic_failover': False, 'site_writes': 'frozen',
            'service_id': self.marker['service_id'],
            'authority_id': self.marker['authority_id']}

    def test_unmarked_legacy_release_keeps_its_existing_behavior(self):
        self.assertIsNone(custody.require_release(self.config, self.old))
        invoke = mock.Mock(return_value={'section': 'guide', 'items': [{}]})
        custody.site_readiness(self.home, invoke)
        invoke.assert_called_once_with('site', 'system.discovery', {'section': 'guide'})

    def test_rollback_rejects_old_lkg_before_pointer_or_restart(self):
        self.pending()
        self.freeze()
        with mock.patch.object(watcher, 'run_command') as run:
            self.worker._restart = lambda value: watcher.Watcher._restart(self.worker, value)
            self.assertFalse(self.worker._rollback('candidate_failed'))
        self.assertEqual(run.call_count, 0)
        self.assertEqual(os.path.realpath(self.worker.current_link), self.new)
        self.assertEqual(self.worker.state['current']['path'], self.new)
        self.assertEqual(self.worker.state['phase'], 'rollback_failed')
        self.assertEqual(self.worker.state['last_error'],
                         'rollback_custody_release_incompatible')
        self.assertIsNotNone(self.worker.state['pending'])

    def test_interrupted_recovery_cannot_restart_old_lkg_even_when_updates_paused(self):
        self.pending()
        self.freeze()
        resumed = Harness(dict(self.config, enabled=False))
        resumed.tick()
        self.assertEqual(resumed.restarts, [])
        self.assertEqual(resumed.polls, 0)
        self.assertEqual(os.path.realpath(resumed.current_link), self.new)
        self.assertEqual(resumed.state['phase'], 'rollback_failed')

    def test_failed_old_rollback_does_not_prevent_a_compatible_repair(self):
        self.pending()
        self.freeze()
        self.worker.head = B
        staged = self.worker._stage
        def repair(commit):
            value = staged(commit)
            self.make_release(value['path'], True)
            return value
        self.worker._stage = repair
        self.worker.tick()
        self.assertEqual(self.worker.restarts, [B])
        self.assertEqual(self.worker.state['last_good']['commit'], B)
        self.assertEqual(self.worker.state['phase'], 'idle')
        self.assertIsNone(self.worker.state['pending'])

    def test_incompatible_candidate_never_switches_pointer(self):
        compatible = {'path': self.new, 'commit': A}
        self.select(self.new)
        self.worker._save(current=compatible, last_good=compatible)
        self.freeze()
        self.worker._activate({'path': self.old, 'commit': B})
        self.assertEqual(self.worker.restarts, [A])
        self.assertEqual(os.path.realpath(self.worker.current_link), self.new)
        self.assertEqual(self.worker.state['rejected_commit'], B)

    def test_preflight_rejects_before_importing_old_candidate(self):
        self.freeze()
        with mock.patch.object(watcher, 'run_command') as run:
            with self.assertRaises(watcher.DeployError) as caught:
                watcher.Watcher._preflight(self.worker, self.old)
        self.assertEqual(caught.exception.code, 'custody_release_incompatible')
        self.assertEqual(run.call_count, 0)

    def test_bootstrap_cannot_promote_incompatible_source(self):
        self.freeze()
        fresh = Harness(dict(self.config, root=os.path.join(self.temporary, 'fresh')))
        with self.assertRaises(watcher.DeployError):
            fresh.initialize(self.old)
        self.assertFalse(os.path.lexists(fresh.current_link))
        self.assertFalse(os.path.exists(fresh.state_path))

    def test_real_stable_launcher_never_executes_legacy_native_or_event_writer(self):
        self.freeze()
        touched = os.path.join(self.temporary, 'legacy-executed')
        unsafe = "open(" + repr(touched) + ", 'w').write('unsafe')\n"
        with open(os.path.join(self.old, 'biomerte', '__main__.py'), 'w') as stream:
            stream.write(unsafe)
        for gateway in (False, True):
            config = dict(self.config)
            if gateway:
                self.make_release(self.old, True)
                with open(os.path.join(self.old, 'deploy', 'gateway-agent.py'), 'w') as stream:
                    stream.write(unsafe)
                config['controller_argv'] = [sys.executable, '-S',
                    'deploy/gateway-agent.py', '--home', self.home, '--origin', 'https://fixture.invalid']
            self.select(self.old)
            path = os.path.join(self.temporary, 'launcher.json')
            save(path, config)
            result = subprocess.run([sys.executable, '-S',
                os.path.join(ROOT, 'deploy', 'controller_autodeploy_exec.py'),
                '--config', path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'custody_release_incompatible', result.stderr)
            self.assertFalse(os.path.exists(touched))

    def test_pinned_compatible_native_and_gateway_pass_without_importing_source(self):
        self.freeze()
        self.select(self.new)
        with mock.patch.object(launcher.os, 'execvpe') as execute, mock.patch.object(launcher.os, 'chdir'):
            launcher.launch(self.config)
        self.assertEqual(execute.call_count, 1)
        config = dict(self.config, controller_argv=[sys.executable, '-S',
            'deploy/gateway-agent.py', '--home', self.home, '--origin', 'https://fixture.invalid'])
        self.assertEqual(custody.require_release(config)['source_node'], 'source-node')
        # A gateway engine from an older overlay is not compatible merely
        # because its entrypoint carries the current protocol declaration.
        with open(os.path.join(self.new, 'deploy', 'mcp_events.py'), 'w') as stream:
            stream.write('# old event engine\n')
        with self.assertRaises(custody.CustodyError):
            custody.require_release(config)

    def test_old_absolute_entrypoint_wrapper_isolated_imports_or_wrong_home_are_rejected(self):
        self.freeze()
        for command in (
            [sys.executable, '-S', os.path.join(self.old, 'deploy', 'gateway-agent.py'),
                '--home', self.home],
            [sys.executable, '-S', '-c', 'import biomerte', '--home', self.home],
            [sys.executable, '-S', '-I', '-m', 'biomerte', '--home', self.home, 'serve'],
            [sys.executable, '-S', '-m', 'biomerte', '--home', self.temporary, 'serve']):
            with self.subTest(command=command):
                with self.assertRaises(custody.CustodyError):
                    custody.require_release(dict(self.config, controller_argv=command), self.new)

    def test_only_the_qualified_selected_trace_wrapper_keeps_native_diagnostics(self):
        self.freeze()
        self.select(self.new)
        path = os.path.join(self.new, 'deploy', 'controller_trace.py')
        with open(path, 'w') as stream:
            stream.write('CUSTODY_PROTOCOL = 1\n')
        log = os.path.join(self.home, 'request-diagnostics.log')
        command = [sys.executable, '-S', 'deploy/controller_trace.py',
                   '--diagnostic-log', log, '--home', self.home, 'serve']
        config = dict(self.config, controller_argv=command)
        with mock.patch.object(launcher.os, 'execvpe') as execute, mock.patch.object(launcher.os, 'chdir'):
            launcher.launch(config)
        self.assertEqual(execute.call_args.args[1], command)
        old = list(command)
        old[2] = os.path.join(self.old, 'controller_trace.py')
        with self.assertRaises(custody.CustodyError):
            custody.require_release(dict(config, controller_argv=old), self.new)
        for offset, replacement in ((3, '--diagnostic-l'), (4, 'relative.log'),
                                    (5, '--hom'), (6, self.temporary)):
            bad = list(command)
            bad[offset] = replacement
            with self.subTest(offset=offset):
                with self.assertRaises(custody.CustodyError):
                    custody.require_release(dict(config, controller_argv=bad), self.new)
        with open(path, 'w') as stream:
            stream.write("raise AssertionError('legacy trace must never execute')\n")
        with self.assertRaises(custody.CustodyError):
            custody.require_release(config, self.new)

    def test_relative_interpreter_is_not_a_pinned_execution_boundary(self):
        self.freeze()
        command = ['python', '-S', '-m', 'biomerte', '--home', self.home, 'serve']
        with self.assertRaises(custody.CustodyError):
            custody.require_release(dict(self.config, controller_argv=command), self.new)

    def test_gateway_abbreviated_home_cannot_override_the_guarded_home(self):
        self.freeze()
        for option in ('--h', '--ho', '--hom', '--ho=', '--hom='):
            command = [sys.executable, '-S', 'deploy/gateway-agent.py',
                       '--home', self.home, '--origin', 'https://fixture.invalid']
            command += ([option + self.temporary] if option.endswith('=')
                        else [option, self.temporary])
            with self.subTest(option=option):
                with self.assertRaises(custody.CustodyError):
                    custody.require_release(dict(self.config, controller_argv=command), self.new)

    def test_missing_invalid_foreign_or_managed_authority_marker_cannot_execute(self):
        self.freeze()
        self.select(self.new)
        mutations = [
            {'format': 2}, {'source_node': 'foreign'}, {'site': 'foreign'},
            {'mode': 'managed-authority'}, {'controller_aliases': []},
            {'source_digest': 'invalid'}, {'authority_id': 'source-node'}]
        for delta in mutations:
            save(self.marker_path, dict(self.marker, **delta))
            with mock.patch.object(launcher.os, 'execvpe') as execute:
                with self.assertRaises(custody.CustodyError):
                    launcher.launch(self.config)
            self.assertEqual(execute.call_count, 0)
        with open(self.marker_path, 'w') as stream:
            stream.write('{')
        with self.assertRaises(custody.CustodyError):
            custody.read_custody(self.home)
        os.unlink(self.marker_path)
        os.symlink(os.path.join(self.temporary, 'missing'), self.marker_path)
        with self.assertRaises(custody.CustodyError):
            custody.read_custody(self.home)

    def test_real_watcher_health_uses_exact_frozen_source_status(self):
        self.freeze()
        response = [{'result': {'structuredContent': dict(self.node, release={'commit': A})}},
                    {'result': {'structuredContent': self.source_status()}}]
        real = watcher.Watcher(self.config)
        with mock.patch.object(watcher, 'request_json', side_effect=response) as request:
            real._health({'path': self.new, 'commit': A})
        operations = [call.args[1]['params']['arguments']['operation']
                      for call in request.call_args_list]
        self.assertEqual(operations, ['system.info', 'cluster.status'])
        for delta in ({'mode': 'managed-controller'}, {'site_writes': 'enabled'},
                      {'node': 'logical-node'}, {'authority_id': 'foreign'},
                      {'service_id': 'foreign'}, {'automatic_failover': True}):
            bad = dict(self.source_status(), **delta)
            with self.subTest(delta=delta):
                with self.assertRaises(custody.CustodyError):
                    custody.site_readiness(self.home, lambda *args: bad)

    def test_marker_change_during_health_cannot_be_accepted(self):
        self.freeze()
        def changed(*args):
            os.unlink(self.marker_path)
            return self.source_status()
        with self.assertRaises(custody.CustodyError):
            custody.site_readiness(self.home, changed)

    def watchdog_config(self):
        key = os.path.join(self.home, 'bridge.key')
        Path(key).write_text('fixture-key')
        return dict(watchdog.DEFAULTS, home=self.home, key=key,
                    origin='https://fixture.invalid')

    def test_frozen_source_watchdog_is_healthy_without_discovery_or_restart(self):
        self.freeze()
        operations = []
        def response(url, payload, token, timeout, access=None):
            if url.endswith('/mcp'):
                operation = payload['params']['arguments']['operation']
                operations.append(operation)
                self.assertNotEqual(operation, 'system.discovery')
                value = (dict(self.node, realm='local') if operation == 'system.info'
                         else self.source_status())
                return {'result': {'structuredContent': value}}
            if url.endswith('/bridge/ha/status'):
                return {'site': self.node['site'], 'nodes': []}
            return {'structuredContent': {'node': self.node['node'], 'health': {'runtime': 'up'}}}
        config = self.watchdog_config()
        observed = watchdog.Probes(config, response).collect()
        recovery = watchdog.Recovery(dict(config, failure_threshold=1))
        restart = mock.Mock()
        self.assertEqual(recovery.observe(observed, restart)['status'], 'healthy')
        self.assertEqual(operations, ['system.info', 'cluster.status'])
        self.assertEqual(restart.call_count, 0)

    def test_actual_watchdog_and_service_restart_paths_reject_old_lkg(self):
        self.freeze()
        plist = os.path.join(self.temporary, 'fixture.plist')
        Path(plist).write_text('fixture')
        config = dict(self.config, service={'manager': 'launchd',
            'label': watchdog.LABELS['controller'], 'domain': 'gui/' + str(os.getuid()),
            'plist': plist})
        selected = os.path.join(self.temporary, 'service.json')
        save(selected, config)
        supervision = dict(self.watchdog_config(), service_configs={'controller': selected})
        with mock.patch.object(watchdog.subprocess, 'run') as run:
            with self.assertRaises(custody.CustodyError):
                watchdog.restart_service('controller', config=supervision)
        self.assertEqual(run.call_count, 0)
        runner = mock.Mock()
        with self.assertRaises(custody.CustodyError):
            service.restart(config, runner=runner)
        self.assertEqual(runner.call_count, 0)
        with mock.patch.object(watchdog.subprocess, 'run') as run:
            with self.assertRaises(watchdog.ProbeError):
                watchdog.restart_service('controller', config=self.watchdog_config())
        self.assertEqual(run.call_count, 0)
        self.select(self.new)
        with mock.patch.object(watchdog.subprocess, 'run') as run:
            watchdog.restart_service('controller', config=supervision)
        self.assertEqual(run.call_count, 2)

    def test_watchdog_installation_copies_its_stable_helper(self):
        fake_home = Path(self.temporary) / 'user'
        (fake_home / 'Library' / 'LaunchAgents').mkdir(parents=True)
        with mock.patch.object(watchdog.Path, 'home', return_value=fake_home):
            watchdog.install(self.home, 'https://fixture.invalid',
                             os.path.join(self.home, 'bridge.key'), sys.executable)
        helper = Path(self.home) / 'recovery' / 'controller_custody.py'
        self.assertEqual(helper.read_bytes(),
                         (Path(ROOT) / 'deploy' / 'controller_custody.py').read_bytes())
        result = subprocess.run([sys.executable, '-S',
            str(helper.with_name('controller_watchdog.py')), '--help'],
            cwd=self.temporary, env={'PATH': os.environ.get('PATH', '')},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
