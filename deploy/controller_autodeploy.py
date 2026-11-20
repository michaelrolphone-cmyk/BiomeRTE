#!/usr/bin/env python3
"""Independent controller branch deployment and rollback (Python 3.4+).

Install this file outside the releases it manages. It never imports biomerte,
initializes a node, modifies node.json, or updates its own installed copy.
"""
from __future__ import print_function

import argparse
import copy
import errno
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import contextmanager
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.parse import urlparse

# This helper is installed beside the recovery program, outside releases.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from controller_custody import CustodyError, require_release, site_readiness

SCHEMA = 'biomerte-autodeploy-release/1'
SHA = re.compile(r'^[0-9a-f]{40}$')
DEFAULTS = {
    'branch': 'main', 'interval': 60, 'git_timeout': 60,
    'command_timeout': 60, 'preflight_timeout': 120,
    'startup_timeout': 120, 'probe_timeout': 10, 'probe_interval': 2,
    'retry_seconds': 60, 'include_paths': ['biomerte', 'examples'],
    'enabled': True
}


class DeployError(Exception):
    def __init__(self, code, permanent=False):
        self.code, self.permanent = code, permanent
        Exception.__init__(self, code)


class Busy(Exception):
    pass


def read_json(path):
    with open(path, encoding='utf-8') as stream:
        return json.load(stream)


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sync_tree(path):
    # Pay the durability cost once for an update, never for unchanged polls.
    for directory, unused, files in os.walk(path, topdown=False):
        for name in files:
            descriptor = os.open(os.path.join(directory, name), os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        sync_directory(directory)


def atomic_json(path, value):
    directory = os.path.dirname(path)
    descriptor, temporary = tempfile.mkstemp(prefix='.state-', dir=directory)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_link(path, target):
    directory = os.path.dirname(path)
    descriptor, temporary = tempfile.mkstemp(prefix='.link-', dir=directory)
    os.close(descriptor)
    os.unlink(temporary)
    try:
        os.symlink(target, temporary)
        os.replace(temporary, path)
        sync_directory(directory)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


@contextmanager
def file_lock(path):
    # A zero-length file is created once; flock does not rewrite it on polls.
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                raise Busy()
            raise
        yield
    finally:
        os.close(descriptor)


def run_command(argv, timeout, cwd=None, env=None, stdout_file=None):
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise DeployError('invalid_command')
    process = subprocess.Popen(argv, cwd=cwd, env=env,
                               stdin=subprocess.DEVNULL,
                               stdout=stdout_file or subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    try:
        output, unused = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # These deadlines concern watcher Git/probe/restart subprocesses only.
        # They never apply to controller capabilities or user jobs.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        process.communicate()
        raise DeployError('command_timeout')
    if process.returncode:
        # Commands/URLs/stderr can contain secrets: publish only an exit code.
        raise DeployError('command_exit_' + str(process.returncode))
    return output or b''


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def request_json(url, payload, token, timeout):
    body = json.dumps(payload).encode('utf-8')
    request = Request(url, data=body, headers={
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + token,
        'User-Agent': 'BiomeRTE-Autodeploy/1'})
    try:
        with build_opener(NoRedirect).open(request, timeout=timeout) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception:
        raise DeployError('authenticated_probe_failed')


def tool_value(reply):
    try:
        result = reply['result']
        if result.get('isError'):
            raise ValueError()
        if isinstance(result.get('structuredContent'), dict):
            return result['structuredContent']
        return json.loads(next(item['text'] for item in result['content']
                               if item['type'] == 'text'))
    except (KeyError, TypeError, ValueError, StopIteration):
        raise DeployError('invalid_probe_response')


def safe_extract(archive, destination):
    with tarfile.open(archive) as source:
        members = source.getmembers()
        for member in members:
            path = os.path.normpath(member.name)
            if (os.path.isabs(path) or path == '..' or path.startswith('../')
                    or not (member.isfile() or member.isdir())):
                raise DeployError('unsafe_archive_member', permanent=True)
        source.extractall(destination, members=members)


class Watcher:
    def __init__(self, config):
        self.config = dict(DEFAULTS, **config)
        for key in ('root', 'home', 'repository', 'python', 'restart_commands'):
            if key not in self.config:
                raise DeployError('missing_config_' + key)
        self.root = os.path.abspath(os.path.expanduser(self.config['root']))
        self.home = os.path.abspath(os.path.expanduser(self.config['home']))
        self.state_path = os.path.join(self.root, 'status.json')
        self.current_link = os.path.join(self.root, 'current')
        self.releases = os.path.join(self.root, 'releases')
        self.lock_path = self.config.get('recovery_lock',
                                         os.path.join(self.root, 'recovery.lock'))
        if not os.path.isfile(os.path.join(self.home, 'node.json')):
            raise DeployError('existing_node_required')
        branch = self.config['branch']
        if not isinstance(branch, str) or not branch or branch.startswith('-'):
            raise DeployError('invalid_branch')
        if not isinstance(self.config['restart_commands'], list) or not self.config['restart_commands']:
            raise DeployError('restart_commands_required')
        for command in self.config['restart_commands']:
            if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
                raise DeployError('invalid_restart_command')
        for key in ('interval', 'git_timeout', 'command_timeout', 'preflight_timeout',
                    'startup_timeout', 'probe_timeout', 'probe_interval', 'retry_seconds'):
            if not isinstance(self.config[key], (float, int)) or self.config[key] <= 0:
                raise DeployError('invalid_' + key)
        for path in self.config['include_paths']:
            if (not isinstance(path, str) or path.startswith(('/', '-'))
                    or '..' in path.split('/') or not path):
                raise DeployError('invalid_include_path')
        self.state = read_json(self.state_path) if os.path.isfile(self.state_path) else None
        if self.state is not None:
            if self.state.get('schema') != 1:
                raise DeployError('unknown_state_schema')
            if self.state.get('root') != self.root or self.state.get('home') != self.home:
                raise DeployError('state_identity_mismatch')
            self._identity()

    def _identity(self):
        config = read_json(os.path.join(self.home, 'node.json'))
        identity = {'node': config['node'], 'site': config['site']}
        if self.state is not None and identity != self.state['identity']:
            raise DeployError('node_identity_changed')
        return config, identity

    def _save(self, **changes):
        updated = dict(self.state, **changes)
        if updated != self.state:
            updated['changed_at'] = time.time()
            atomic_json(self.state_path, updated)
            self.state = updated

    def _env(self, release=None):
        environment = dict(os.environ)
        environment['GIT_TERMINAL_PROMPT'] = '0'
        if release is not None:
            environment['PYTHONPATH'] = release
            environment['PYTHONDONTWRITEBYTECODE'] = '1'
        return environment

    def _expand(self, argv, release):
        values = {'root': self.root, 'home': self.home,
                  'release': release['path'], 'commit': release.get('commit') or ''}
        result = []
        for part in argv:
            for key, value in values.items():
                part = part.replace('{' + key + '}', value)
            result.append(part)
        return result

    def initialize(self, existing_release):
        if self.state is not None:
            raise DeployError('already_initialized')
        target = os.path.realpath(existing_release)
        if not os.path.isfile(os.path.join(target, 'biomerte', '__init__.py')):
            raise DeployError('bootstrap_package_missing')
        if os.path.lexists(self.current_link):
            if not os.path.islink(self.current_link) or os.path.realpath(self.current_link) != target:
                raise DeployError('current_path_already_exists')
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        os.makedirs(self.releases, mode=0o700, exist_ok=True)
        config, identity = self._identity()
        bootstrap = {'commit': None, 'path': target}
        self._guard(bootstrap)
        # The existing running controller, its identity and request path must
        # actually work before they become the initial rollback destination.
        self._health(bootstrap)
        atomic_link(self.current_link, target)
        self.state = {'schema': 1, 'root': self.root, 'home': self.home,
                      'identity': identity, 'phase': 'idle',
                      'bootstrap': bootstrap, 'current': bootstrap,
                      'last_good': bootstrap, 'previous_good': None,
                      'desired_commit': None, 'attempted_commit': None,
                      'rejected_commit': None, 'rejected_release': None,
                      'pending': None, 'last_error': None, 'poll_error': None, 'next_retry_at': 0,
                      'changed_at': time.time()}
        atomic_json(self.state_path, self.state)
        return self.state

    def _remote_head(self):
        output = run_command(
            ['git', 'ls-remote', '--exit-code', self.config['repository'],
             'refs/heads/' + self.config['branch']],
            self.config['git_timeout'], env=self._env()).decode('ascii')
        matches = [line.split()[0] for line in output.splitlines()
                   if len(line.split()) == 2 and
                   line.split()[1] == 'refs/heads/' + self.config['branch']]
        if len(matches) != 1 or not SHA.match(matches[0]):
            raise DeployError('branch_head_missing')
        return matches[0]

    def _valid_release(self, path, commit):
        try:
            marker = read_json(os.path.join(path, '.biomerte-release.json'))
            return marker == {'schema': SCHEMA, 'owner': self.root, 'commit': commit}
        except (OSError, ValueError):
            return False

    def _guard(self, release):
        try:
            return require_release(self.config, release['path'])
        except CustodyError as error:
            raise DeployError(error.code, permanent=True)

    def _preflight(self, path):
        self._guard({'path': path})
        # Import the real controller dependency closure with this host's Python.
        # Optional rover/async providers may require a newer Python and are not
        # loaded by the controller; compiling every file would reject valid Pi
        # releases. Imports do not instantiate Application or initialize state.
        program = "import biomerte.app; import biomerte.__main__"
        try:
            run_command([self.config['python'], '-S', '-B', '-c', program],
                        self.config['preflight_timeout'], cwd=path,
                        env=self._env(path))
            for command in self.config.get('preflight_commands', []):
                run_command(self._expand(command, {'path': path, 'commit': ''}),
                            self.config['preflight_timeout'], cwd=path,
                            env=self._env(path))
        except Exception:
            raise DeployError('candidate_preflight_failed', permanent=True)

    def _stage(self, commit):
        destination = os.path.join(self.releases, commit)
        if os.path.lexists(destination):
            if os.path.islink(destination) or not self._valid_release(destination, commit):
                raise DeployError('release_path_conflict')
            self._preflight(destination)
            return {'commit': commit, 'path': destination}
        temporary = tempfile.mkdtemp(prefix='.prepare-', dir=self.root)
        try:
            atomic_json(os.path.join(temporary, '.watcher-owned.json'),
                        {'owner': self.root})
            repository = os.path.join(temporary, 'repo.git')
            candidate = os.path.join(temporary, 'release')
            archive = os.path.join(temporary, 'source.tar')
            os.mkdir(candidate)
            environment = self._env()
            timeout = self.config['git_timeout']
            run_command(['git', 'init', '--bare', repository], timeout, env=environment)
            # Fetch the branch, then deploy the fetched immutable commit. If it
            # moved after ls-remote, defer to the next poll rather than mixing refs.
            run_command(['git', '--git-dir=' + repository, 'fetch', '--depth=1',
                         '--no-tags', self.config['repository'],
                         'refs/heads/' + self.config['branch']], timeout, env=environment)
            fetched = run_command(['git', '--git-dir=' + repository, 'rev-parse',
                                   'FETCH_HEAD'], timeout, env=environment).decode('ascii').strip()
            if fetched != commit:
                raise DeployError('branch_moved_during_fetch')
            with open(archive, 'wb') as output:
                run_command(['git', '--git-dir=' + repository, 'archive',
                             '--format=tar', commit, '--'] + self.config['include_paths'],
                            timeout, env=environment, stdout_file=output)
            safe_extract(archive, candidate)
            if not os.path.isfile(os.path.join(candidate, 'biomerte', '__init__.py')):
                raise DeployError('candidate_package_missing', permanent=True)
            self._preflight(candidate)
            atomic_json(os.path.join(candidate, '.biomerte-release.json'),
                        {'schema': SCHEMA, 'owner': self.root, 'commit': commit})
            sync_tree(candidate)
            os.rename(candidate, destination)
            sync_directory(self.releases)
            return {'commit': commit, 'path': destination}
        finally:
            shutil.rmtree(temporary)

    def _restart(self, release):
        if (self._guard(release) is not None
                and os.path.realpath(self.current_link) != os.path.realpath(release['path'])):
            raise DeployError('custody_release_pointer_changed')
        for command in self.config['restart_commands']:
            run_command(self._expand(command, release),
                        self.config['command_timeout'], env=self._env())

    def _health(self, release):
        self._guard(release)
        config, identity = self._identity()
        origin = self.config.get('local_origin',
                                  'http://127.0.0.1:' + str(config.get('port', 8740)))
        parsed = urlparse(origin)
        if parsed.scheme not in ('http', 'https') or parsed.hostname not in ('127.0.0.1', '::1', 'localhost'):
            raise DeployError('local_probe_must_use_loopback')
        def invoke(realm, operation, arguments):
            payload = {'jsonrpc': '2.0', 'id': 'autodeploy-readiness',
                       'method': 'tools/call', 'params': {
                           'name': 'invoke', 'arguments': {
                               'realm': realm, 'operation': operation,
                               'arguments': arguments}}}
            return tool_value(request_json(origin.rstrip('/') + '/mcp', payload,
                                           config['admin_token'],
                                           self.config['probe_timeout']))
        info = invoke('local', 'system.info', {})
        if info.get('node') != identity['node'] or info.get('site') != identity['site']:
            raise DeployError('local_identity_mismatch')
        if release.get('commit') and info.get('release', {}).get('commit') != release['commit']:
            raise DeployError('running_release_mismatch')
        if not release.get('commit') and self.config.get('bootstrap_source_command'):
            run_command(self._expand(self.config['bootstrap_source_command'], release),
                        self.config['probe_timeout'], env=self._env())
        try:
            site_readiness(self.home, invoke)
        except CustodyError as error:
            raise DeployError(error.code)
        if self.config.get('health_command'):
            run_command(self._expand(self.config['health_command'], release),
                        self.config['probe_timeout'], env=self._env())
        return info

    def _wait_healthy(self, release):
        deadline = time.monotonic() + self.config['startup_timeout']
        while True:
            try:
                return self._health(release)
            except Exception as error:
                if time.monotonic() >= deadline:
                    raise DeployError(getattr(error, 'code', 'readiness_failed'))
                time.sleep(min(self.config['probe_interval'],
                               max(0, deadline - time.monotonic())))

    def _rollback(self, reason, preserve_rejection=False):
        pending = self.state['pending']
        previous = pending['previous']
        failed = pending['candidate']
        rejected_commit = self.state.get('rejected_commit') if preserve_rejection else failed['commit']
        rejected_release = self.state.get('rejected_release') if preserve_rejection else failed
        self._save(phase='rolling_back', last_error=reason,
                   rejected_commit=rejected_commit, rejected_release=rejected_release)
        try:
            # The marker can make a formerly healthy LKG permanently unsafe.
            # Reject it before changing the boot pointer or issuing a restart.
            self._guard(previous)
            atomic_link(self.current_link, previous['path'])
            self._save(current=previous)
            self._restart(previous)
            self._wait_healthy(previous)
        except Exception as error:
            self._save(phase='rollback_failed',
                       last_error='rollback_' + getattr(error, 'code', type(error).__name__))
            return False
        self._save(phase='idle', pending=None, current=previous,
                   last_good=previous, last_error=reason, next_retry_at=0)
        self._prune()
        return True

    def _recover(self):
        if self.state.get('pending'):
            with file_lock(self.lock_path):
                preserve = (self.state['phase'] in ('rollback_failed', 'staging_repair', 'rolling_back')
                            and self.state.get('rejected_commit') is not None)
                return self._rollback('interrupted_deployment_rolled_back',
                                      preserve_rejection=preserve)
        if self.state['phase'] == 'staging':
            self._save(phase='idle', last_error='interrupted_staging', next_retry_at=0)
        return True

    def _activate(self, candidate):
        with file_lock(self.lock_path):
            previous = self.state['last_good']
            self._save(phase='activating',
                       pending={'candidate': candidate, 'previous': previous})
            try:
                self._guard(candidate)
                atomic_link(self.current_link, candidate['path'])
                self._save(current=candidate, phase='verifying')
                self._restart(candidate)
                self._wait_healthy(candidate)
            except Exception as error:
                return self._rollback(getattr(error, 'code', type(error).__name__))
            self._save(phase='idle', current=candidate, last_good=candidate,
                       previous_good=previous, pending=None, last_error=None,
                       rejected_commit=None, rejected_release=None, next_retry_at=0)
            self._prune()
            return True

    def _prune(self):
        keep = set()
        for key in ('bootstrap', 'current', 'last_good', 'previous_good', 'rejected_release'):
            value = self.state.get(key)
            if value:
                keep.add(os.path.realpath(value['path']))
        if self.state.get('pending'):
            for value in self.state['pending'].values():
                keep.add(os.path.realpath(value['path']))
        for name in os.listdir(self.releases):
            path = os.path.join(self.releases, name)
            if (SHA.match(name) and not os.path.islink(path)
                    and os.path.realpath(path) not in keep
                    and self._valid_release(path, name)):
                shutil.rmtree(path)
        # Only abandoned staging areas explicitly marked as ours are eligible.
        for name in os.listdir(self.root):
            if not name.startswith('.prepare-'):
                continue
            path = os.path.join(self.root, name)
            if os.path.islink(path):
                continue
            try:
                owned = read_json(os.path.join(path, '.watcher-owned.json'))
            except (OSError, ValueError):
                continue
            if owned == {'owner': self.root}:
                shutil.rmtree(path)

    def tick(self):
        if self.state is None:
            raise DeployError('initialize_existing_release_first')
        self._identity()
        # Recovery precedes Git/network work, including when updates are paused.
        self._recover()
        if not self.config.get('enabled', True):
            return self.state
        if not os.path.islink(self.current_link):
            raise DeployError('current_link_missing')
        if os.path.realpath(self.current_link) != os.path.realpath(self.state['current']['path']):
            raise DeployError('current_link_changed_externally')
        try:
            head = self._remote_head()
        except Exception as error:
            self._save(poll_error=getattr(error, 'code', type(error).__name__))
            return self.state
        if self.state.get('poll_error') is not None:
            self._save(poll_error=None)
        if head != self.state.get('desired_commit'):
            self._save(desired_commit=head, next_retry_at=0)
        if head in (self.state['current'].get('commit'), self.state.get('rejected_commit')):
            return self.state
        if time.time() < self.state.get('next_retry_at', 0):
            return self.state
        pending_recovery = self.state.get('pending') is not None
        self._save(phase='staging_repair' if pending_recovery else 'staging',
                   attempted_commit=head, last_error=None)
        try:
            candidate = self._stage(head)
        except Exception as error:
            permanent = getattr(error, 'permanent', False)
            self._save(phase='rollback_failed' if pending_recovery else 'idle',
                       last_error=getattr(error, 'code', type(error).__name__),
                       rejected_commit=head if permanent else self.state.get('rejected_commit'),
                       next_retry_at=0 if permanent else time.time() + self.config['retry_seconds'])
            return self.state
        self._activate(candidate)
        return self.state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--initialize', metavar='EXISTING_RELEASE')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--status', action='store_true')
    options = parser.parse_args(argv)
    config = read_json(os.path.abspath(options.config))
    watcher = Watcher(config)
    if options.status:
        print(json.dumps(watcher.state, sort_keys=True))
        return 0
    os.makedirs(watcher.root, mode=0o700, exist_ok=True)
    with file_lock(os.path.join(watcher.root, 'watcher.lock')):
        if options.initialize:
            with file_lock(watcher.lock_path):
                print(json.dumps(watcher.initialize(options.initialize), sort_keys=True))
            return 0
        last_report = None
        while True:
            try:
                # Accept a local configuration change without changing the
                # independently installed watcher program.
                watcher = Watcher(read_json(os.path.abspath(options.config)))
                state = watcher.tick()
                report = {'phase': state['phase'],
                          'current_commit': state['current'].get('commit'),
                          'desired_commit': state.get('desired_commit'),
                          'last_error': state.get('last_error'),
                          'poll_error': state.get('poll_error')}
            except Busy:
                report = {'phase': 'recovery_lock_busy'}
            except Exception as error:
                report = {'phase': 'watcher_error',
                          'last_error': getattr(error, 'code', type(error).__name__)}
            if options.once or report != last_report:
                print(json.dumps(report, sort_keys=True))
                sys.stdout.flush()
                last_report = report
            if options.once:
                return 0 if report.get('phase') == 'idle' and not report.get('last_error') and not report.get('poll_error') else 1
            time.sleep(watcher.config['interval'])


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Busy:
        sys.stderr.write('Another updater owns watcher.lock\n')
        sys.exit(75)
    except Exception as error:
        sys.stderr.write(getattr(error, 'code', type(error).__name__) + '\n')
        sys.exit(1)
