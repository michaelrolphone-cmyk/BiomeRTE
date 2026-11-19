"""Recovery behavior for the independent branch watcher; no production service use."""
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_module(name, path):
    module = types.ModuleType(name)
    module.__file__ = path
    with open(path, 'rb') as stream:
        exec(compile(stream.read(), path, 'exec'), module.__dict__)
    return module


watcher = load_module('controller_autodeploy',
                      os.path.join(ROOT, 'deploy', 'controller_autodeploy.py'))
A, B, C, D = [letter * 40 for letter in ('a', 'b', 'c', 'd')]


def save(path, value):
    with open(path, 'w') as stream:
        json.dump(value, stream)


class Harness(watcher.Watcher):
    def __init__(self, config):
        self.head = A
        self.staged = []
        self.restarts = []
        self.bad = set()
        self.polls = 0
        watcher.Watcher.__init__(self, config)

    def _remote_head(self):
        self.polls += 1
        return self.head

    def _stage(self, commit):
        self.staged.append(commit)
        path = os.path.join(self.releases, commit)
        os.makedirs(path, exist_ok=True)
        save(os.path.join(path, '.biomerte-release.json'),
             {'schema': watcher.SCHEMA, 'owner': self.root, 'commit': commit})
        return {'commit': commit, 'path': path}

    def _restart(self, release):
        self.restarts.append(release['commit'])

    def _health(self, release):
        if release['commit'] in self.bad:
            raise watcher.DeployError('fixture_not_ready')
        return {'node': 'fixture-node', 'release': {'commit': release['commit']}}

    def _wait_healthy(self, release):
        return self._health(release)


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = os.path.realpath(tempfile.mkdtemp(prefix='autodeploy-test-'))
        self.home = os.path.join(self.temporary, 'controller-home')
        self.original = os.path.join(self.temporary, 'original-release')
        self.root = os.path.join(self.temporary, 'updater')
        os.makedirs(self.home)
        os.makedirs(os.path.join(self.original, 'biomerte'))
        with open(os.path.join(self.original, 'biomerte', '__init__.py'), 'w') as stream:
            stream.write('')
        save(os.path.join(self.home, 'node.json'),
             {'node': 'fixture-node', 'site': 'fixture-site', 'admin_token': 'SECRET'})
        self.config = {'root': self.root, 'home': self.home,
                       'repository': '/fixture/repository', 'python': sys.executable,
                       'restart_commands': [['/fixture/restart']],
                       'startup_timeout': .01, 'probe_interval': .001}
        self.worker = Harness(self.config)
        self.worker.initialize(self.original)

    def tearDown(self):
        shutil.rmtree(self.temporary)

    def test_success_and_unchanged_branch_perform_no_writes_or_restarts(self):
        self.worker.tick()
        with open(self.worker.state_path, 'rb') as stream:
            before = stream.read()
        before_mtime = os.stat(self.worker.state_path).st_mtime_ns
        with mock.patch.object(watcher, 'atomic_json', wraps=watcher.atomic_json) as write:
            self.worker.tick()
        self.assertEqual(write.call_count, 0)
        self.assertEqual(self.worker.staged, [A])
        self.assertEqual(self.worker.restarts, [A])
        self.assertEqual(self.worker.polls, 2)
        self.assertEqual(os.stat(self.worker.state_path).st_mtime_ns, before_mtime)
        with open(self.worker.state_path, 'rb') as stream:
            self.assertEqual(stream.read(), before)

    def test_failed_start_rolls_back_and_next_branch_patch_recovers(self):
        self.worker.bad.add(A)
        self.worker.tick()
        self.assertEqual(self.worker.state['rejected_commit'], A)
        self.assertEqual(self.worker.state['current']['path'], self.original)
        self.assertEqual(self.worker.restarts, [A, None])
        self.worker.tick()
        self.assertEqual(self.worker.staged, [A])
        self.assertEqual(self.worker.restarts, [A, None])
        self.worker.head = B
        self.worker.tick()
        self.assertEqual(self.worker.state['last_good']['commit'], B)
        self.assertEqual(self.worker.state['phase'], 'idle')
        self.assertIsNone(self.worker.state['pending'])

    def test_interruption_after_link_switch_rolls_back_before_network(self):
        candidate = self.worker._stage(A)
        self.worker._save(phase='verifying', attempted_commit=A,
                          pending={'candidate': candidate, 'previous': self.worker.state['last_good']})
        watcher.atomic_link(self.worker.current_link, candidate['path'])
        self.worker._save(current=candidate)
        resumed = Harness(self.config)
        with mock.patch.object(resumed, '_remote_head',
                               side_effect=watcher.DeployError('network_offline')):
            resumed.tick()
        self.assertEqual(resumed.restarts, [None])
        self.assertEqual(os.path.realpath(resumed.current_link), self.original)
        self.assertIsNone(resumed.state['pending'])
        self.assertEqual(resumed.state['rejected_commit'], A)

    def test_interruption_before_link_switch_preserves_bootstrap(self):
        candidate = self.worker._stage(A)
        self.worker._save(phase='activating',
                          pending={'candidate': candidate, 'previous': self.worker.state['last_good']})
        resumed = Harness(self.config)
        resumed.tick()
        self.assertEqual(resumed.restarts, [None])
        self.assertEqual(resumed.state['current']['path'], self.original)
        self.assertIsNone(resumed.state['pending'])

    def test_failed_rollback_does_not_block_a_new_repair_commit(self):
        self.worker.bad.update([A, None])
        self.worker.tick()
        self.assertEqual(self.worker.state['phase'], 'rollback_failed')
        self.assertIsNotNone(self.worker.state['pending'])
        self.worker.head = B
        self.worker.tick()
        self.assertEqual(self.worker.state['current']['commit'], B)
        self.assertEqual(self.worker.state['last_good']['commit'], B)
        self.assertIsNone(self.worker.state['pending'])

    def test_preflight_failure_does_not_switch_or_restart(self):
        with mock.patch.object(self.worker, '_stage',
                               side_effect=watcher.DeployError('bad_syntax', permanent=True)):
            self.worker.tick()
        self.assertEqual(self.worker.state['current']['path'], self.original)
        self.assertFalse(self.worker.restarts)
        self.assertEqual(self.worker.state['rejected_commit'], A)
        with mock.patch.object(self.worker, '_stage') as stage:
            self.worker.tick()
        self.assertEqual(stage.call_count, 0)

    def test_transient_fetch_is_retried_later_but_new_head_is_immediate(self):
        with mock.patch.object(self.worker, '_stage',
                               side_effect=watcher.DeployError('network_unavailable')):
            self.worker.tick()
        self.assertIsNone(self.worker.state['rejected_commit'])
        with mock.patch.object(self.worker, '_stage') as stage:
            self.worker.tick()
        self.assertEqual(stage.call_count, 0)
        self.worker.head = B
        self.worker.tick()
        self.assertEqual(self.worker.state['current']['commit'], B)

    def test_pausing_updates_still_recovers_pending_switch(self):
        candidate = self.worker._stage(A)
        self.worker._save(phase='activating',
                          pending={'candidate': candidate, 'previous': self.worker.state['last_good']})
        self.worker.config['enabled'] = False
        self.worker.tick()
        self.assertEqual(self.worker.restarts, [None])
        self.assertIsNone(self.worker.state['pending'])
        self.assertEqual(self.worker.polls, 0)

    def test_lock_excludes_another_recovery_and_preserves_current(self):
        with watcher.file_lock(self.worker.lock_path):
            with self.assertRaises(watcher.Busy):
                self.worker.tick()
        self.assertEqual(os.path.realpath(self.worker.current_link), self.original)
        self.assertFalse(self.worker.restarts)

    def test_prune_only_owned_obsolete_releases_and_preserve_external_paths(self):
        self.worker.tick()
        self.worker.head = B
        self.worker.tick()
        foreign = os.path.join(self.worker.releases, D)
        os.makedirs(foreign)
        save(os.path.join(foreign, 'user-data.json'), {'keep': True})
        self.worker.head = C
        self.worker.tick()
        self.assertFalse(os.path.exists(os.path.join(self.worker.releases, A)))
        self.assertTrue(os.path.isdir(os.path.join(self.worker.releases, B)))
        self.assertTrue(os.path.isdir(os.path.join(self.worker.releases, C)))
        self.assertTrue(os.path.isdir(foreign))
        self.assertTrue(os.path.isdir(self.original))

    def test_node_identity_change_is_detected_without_reinitialization(self):
        save(os.path.join(self.home, 'node.json'),
             {'node': 'changed', 'site': 'fixture-site', 'admin_token': 'SECRET'})
        with self.assertRaises(watcher.DeployError) as caught:
            self.worker.tick()
        self.assertEqual(caught.exception.code, 'node_identity_changed')
        self.assertFalse(self.worker.restarts)
        self.assertEqual(watcher.read_json(os.path.join(self.home, 'node.json'))['node'], 'changed')

    def test_local_readiness_requires_correct_running_commit(self):
        real = watcher.Watcher(self.config)
        responses = [
            {'result': {'structuredContent': {
                'node': 'fixture-node', 'site': 'fixture-site', 'release': {'commit': B}}}}
        ]
        with mock.patch.object(watcher, 'request_json', side_effect=responses):
            with self.assertRaises(watcher.DeployError) as caught:
                real._health({'path': self.original, 'commit': A})
        self.assertEqual(caught.exception.code, 'running_release_mismatch')

    def test_external_readiness_failure_is_not_accepted_as_success(self):
        config = dict(self.config, health_command=['/fixture/gateway-check'])
        real = watcher.Watcher(config)
        responses = [
            {'result': {'structuredContent': {
                'node': 'fixture-node', 'site': 'fixture-site', 'release': {'commit': A}}}},
            {'result': {'structuredContent': {'section': 'guide', 'items': [{}]}}}
        ]
        with mock.patch.object(watcher, 'request_json', side_effect=responses):
            with mock.patch.object(watcher, 'run_command',
                                   side_effect=watcher.DeployError('gateway_unavailable')):
                with self.assertRaises(watcher.DeployError):
                    real._health({'path': self.original, 'commit': A})

    def test_local_admin_token_cannot_be_sent_to_non_loopback_origin(self):
        real = watcher.Watcher(dict(self.config, local_origin='https://example.invalid'))
        with mock.patch.object(watcher, 'request_json') as request:
            with self.assertRaises(watcher.DeployError):
                real._health({'path': self.original, 'commit': A})
        self.assertEqual(request.call_count, 0)

    def test_real_git_staging_keeps_package_resources_and_removes_fetch_cache(self):
        repository = os.path.join(self.temporary, 'repository')
        os.makedirs(os.path.join(repository, 'biomerte', 'static'))
        os.makedirs(os.path.join(repository, 'examples'))
        with open(os.path.join(repository, 'examples', 'capability-followup.bpmn'), 'w') as stream:
            stream.write('<definitions />')
        for name in ('__init__.py', 'app.py', '__main__.py'):
            with open(os.path.join(repository, 'biomerte', name), 'w') as stream:
                stream.write('# isolated fixture\n')
        save(os.path.join(repository, 'biomerte', 'workspace-contracts.json'), {'fixture': True})
        with open(os.path.join(repository, 'biomerte', 'static', 'index.html'), 'w') as stream:
            stream.write('<p>fixture</p>')
        with open(os.devnull, 'wb') as quiet:
            subprocess.check_call(['git', 'init', repository], stdout=quiet, stderr=quiet)
            subprocess.check_call(['git', '-C', repository, 'checkout', '-b', 'main'],
                                  stdout=quiet, stderr=quiet)
            subprocess.check_call(['git', '-C', repository, 'add', '.'], stdout=quiet, stderr=quiet)
            subprocess.check_call(['git', '-C', repository, '-c', 'user.name=Fixture',
                                   '-c', 'user.email=fixture@example.invalid',
                                   'commit', '-m', 'fixture'], stdout=quiet, stderr=quiet)
        commit = subprocess.check_output(['git', '-C', repository, 'rev-parse', 'HEAD']).decode().strip()
        self.worker.config['repository'] = repository
        self.worker._remote_head = lambda: watcher.Watcher._remote_head(self.worker)
        self.worker._stage = lambda value: watcher.Watcher._stage(self.worker, value)
        self.worker.tick()
        self.assertEqual(self.worker.state['current']['commit'], commit)
        target = self.worker.state['current']['path']
        self.assertTrue(os.path.isfile(os.path.join(target, 'biomerte', 'static', 'index.html')))
        self.assertTrue(os.path.isfile(os.path.join(target, 'biomerte', 'workspace-contracts.json')))
        self.assertTrue(os.path.isfile(os.path.join(target, 'examples', 'capability-followup.bpmn')))
        self.assertFalse(any(name.startswith('.prepare-') for name in os.listdir(self.root)))
        self.assertFalse(os.path.exists(os.path.join(target, '.git')))

    def test_archive_link_cannot_escape_release(self):
        archive = os.path.join(self.temporary, 'unsafe.tar')
        with tarfile.open(archive, 'w') as output:
            member = tarfile.TarInfo('biomerte/escape')
            member.type = tarfile.SYMTYPE
            member.linkname = '../../elsewhere'
            output.addfile(member)
        with self.assertRaises(watcher.DeployError):
            watcher.safe_extract(archive, self.original)

    def test_process_release_identity_is_frozen_before_current_link_changes(self):
        first = os.path.join(self.temporary, 'first')
        second = os.path.join(self.temporary, 'second')
        for path, commit in ((first, A), (second, B)):
            os.makedirs(os.path.join(path, 'biomerte'))
            shutil.copyfile(os.path.join(ROOT, 'biomerte', 'release.py'),
                            os.path.join(path, 'biomerte', 'release.py'))
            save(os.path.join(path, '.biomerte-release.json'),
                 {'schema': watcher.SCHEMA, 'commit': commit})
        link = os.path.join(self.temporary, 'source')
        os.symlink(first, link)
        module = load_module('release_fixture', os.path.join(link, 'biomerte', 'release.py'))
        watcher.atomic_link(link, second)
        self.assertEqual(module.identity()['commit'], A)
        self.assertEqual(module.identity()['pid'], os.getpid())


    def test_poll_error_is_durable_without_rewriting_each_failed_poll(self):
        with mock.patch.object(self.worker, '_remote_head',
                               side_effect=watcher.DeployError('network_offline')):
            self.worker.tick()
            self.assertEqual(watcher.read_json(self.worker.state_path)['poll_error'], 'network_offline')
            with mock.patch.object(watcher, 'atomic_json', wraps=watcher.atomic_json) as write:
                self.worker.tick()
            self.assertEqual(write.call_count, 0)
        self.worker.tick()
        self.assertIsNone(self.worker.state['poll_error'])
        self.assertEqual(self.worker.state['current']['commit'], A)

    def test_repair_staging_failure_retains_failed_rollback_transaction(self):
        self.worker.bad.update([A, None])
        self.worker.tick()
        pending = self.worker.state['pending']
        self.worker.head = B
        with mock.patch.object(self.worker, '_stage',
                               side_effect=watcher.DeployError('network_unavailable')):
            self.worker.tick()
        self.assertEqual(self.worker.state['phase'], 'rollback_failed')
        self.assertEqual(self.worker.state['pending'], pending)
        self.assertEqual(self.worker.state['pending']['candidate']['commit'], A)

    def test_bootstrap_source_proof_failure_never_initializes_state(self):
        root = os.path.join(self.temporary, 'another-updater')
        config = dict(self.config, root=root, bootstrap_source_command=['/fixture/source-check'])
        fresh = watcher.Watcher(config)
        reply = {'result': {'structuredContent': {'node': 'fixture-node', 'site': 'fixture-site'}}}
        with mock.patch.object(watcher, 'request_json', return_value=reply):
            with mock.patch.object(watcher, 'run_command',
                                   side_effect=watcher.DeployError('wrong_process_source')):
                with self.assertRaises(watcher.DeployError):
                    fresh.initialize(self.original)
        self.assertFalse(os.path.exists(os.path.join(root, 'status.json')))
        self.assertFalse(os.path.lexists(os.path.join(root, 'current')))


    def test_watcher_command_timeout_is_bounded(self):
        with self.assertRaises(watcher.DeployError) as caught:
            watcher.run_command([sys.executable, '-c', 'import time; time.sleep(5)'], .02)
        self.assertEqual(caught.exception.code, 'command_timeout')

    def test_command_failure_does_not_publish_secret_stderr_or_arguments(self):
        with self.assertRaises(watcher.DeployError) as caught:
            watcher.run_command([sys.executable, '-c',
                "import sys; sys.stderr.write('fixture-secret'); sys.exit(7)"], 5)
        self.assertEqual(caught.exception.code, 'command_exit_7')
        self.assertNotIn('fixture-secret', str(caught.exception))


    def test_launcher_pins_lazy_imports_after_current_changes(self):
        second = os.path.join(self.temporary, 'second-source')
        os.makedirs(os.path.join(second, 'biomerte'))
        for root, value in ((self.original, 'first'), (second, 'second')):
            with open(os.path.join(root, 'fixture_payload.py'), 'w') as stream:
                stream.write('VALUE = ' + repr(value) + '\n')
        with open(os.path.join(second, 'biomerte', '__init__.py'), 'w') as stream:
            stream.write('')
        ready = os.path.join(self.temporary, 'child-ready.json')
        trigger = os.path.join(self.temporary, 'child-go')
        result = os.path.join(self.temporary, 'child-result.json')
        # Move cwd away before the lazy import so only the physical PYTHONPATH,
        # not an incidental current-directory entry, can select the first source.
        program = (
            "import json,os,sys,time; os.chdir(sys.argv[4]); "
            "json.dump({'path':sys.path,'argv':sys.argv[1:]},open(sys.argv[1]+'.tmp','w')); os.rename(sys.argv[1]+'.tmp',sys.argv[1]); "
            "\nwhile not os.path.exists(sys.argv[2]): time.sleep(.01)\n"
            "import fixture_payload; "
            "json.dump({'value':fixture_payload.VALUE},open(sys.argv[3],'w'))")
        config = dict(self.config, controller_argv=[
            sys.executable, '-S', '-c', program, ready, trigger, result, self.temporary])
        config_path = os.path.join(self.temporary, 'launcher-config.json')
        save(config_path, config)
        process = subprocess.Popen([sys.executable, '-S',
            os.path.join(ROOT, 'deploy', 'controller_autodeploy_exec.py'),
            '--config', config_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            import time
            deadline = time.monotonic() + 5
            while not os.path.exists(ready) and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(os.path.exists(ready), 'launcher child did not start')
            self.assertIn(self.original, watcher.read_json(ready)['path'])
            watcher.atomic_link(self.worker.current_link, second)
            with open(trigger, 'w') as stream:
                stream.write('go')
            output, error = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, error.decode('utf-8', 'replace'))
            self.assertEqual(watcher.read_json(result)['value'], 'first')
            self.assertEqual(watcher.read_json(ready)['argv'],
                             [ready, trigger, result, self.temporary])
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_launcher_refuses_missing_node_without_running_command(self):
        config = dict(self.config, home=os.path.join(self.temporary, 'missing'),
                      controller_argv=[sys.executable, '-c', "print('SHOULD NOT RUN')"])
        config_path = os.path.join(self.temporary, 'launcher-config.json')
        save(config_path, config)
        process = subprocess.Popen([sys.executable, '-S',
            os.path.join(ROOT, 'deploy', 'controller_autodeploy_exec.py'),
            '--config', config_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output, error = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(output, b'')
        self.assertIn(b'existing_node_required', error)


    def test_bootstrap_rollback_requires_effective_source_proof(self):
        real = watcher.Watcher(dict(self.config, bootstrap_source_command=['/fixture/source-check']))
        reply = {'result': {'structuredContent': {
            'node': 'fixture-node', 'site': 'fixture-site', 'release': {'commit': A}}}}
        with mock.patch.object(watcher, 'request_json', return_value=reply):
            with mock.patch.object(watcher, 'run_command',
                                   side_effect=watcher.DeployError('wrong_process_source')):
                with self.assertRaises(watcher.DeployError) as caught:
                    real._health(real.state['bootstrap'])
        self.assertEqual(caught.exception.code, 'wrong_process_source')


    def test_older_pending_rollback_does_not_forget_rejected_repair(self):
        self.worker.bad.update([A, None])
        self.worker.tick()
        self.worker.head = B
        with mock.patch.object(self.worker, '_stage',
                               side_effect=watcher.DeployError('bad_repair', permanent=True)):
            self.worker.tick()
        self.assertEqual(self.worker.state['rejected_commit'], B)
        with mock.patch.object(self.worker, '_stage') as stage:
            self.worker.tick()
        self.assertEqual(stage.call_count, 0)
        self.assertEqual(self.worker.state['rejected_commit'], B)
        self.assertEqual(self.worker.state['pending']['candidate']['commit'], A)
        # A watcher crash during that older rollback must preserve B as well.
        self.worker._save(phase='rolling_back')
        with mock.patch.object(self.worker, '_stage') as stage:
            self.worker.tick()
        self.assertEqual(stage.call_count, 0)
        self.assertEqual(self.worker.state['rejected_commit'], B)


if __name__ == '__main__':
    unittest.main()
