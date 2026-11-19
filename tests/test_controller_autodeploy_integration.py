"""Real Git, launcher processes and authenticated HTTP recovery (Python 3.4+).

All processes and two ephemeral loopback listeners belong to this fixture.
No system service, production controller, external Git host or real node is used.
BIOMERTE_AUTODEPLOY_SOURCE optionally selects a frozen source tree to qualify.
"""
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import uuid
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = os.environ.get('BIOMERTE_AUTODEPLOY_SOURCE',
                      os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Imports always succeed; only starting the intentionally broken release fails.
CONTROLLER = r'''
import argparse
import json
import os
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer
from .behavior import FAIL_STARTUP, LABEL
from .release import identity

def run():
    parser = argparse.ArgumentParser()
    parser.add_argument('--home')
    parser.add_argument('--socket-fd', type=int)
    parser.add_argument('--events')
    args = parser.parse_args()
    with open(os.path.join(args.home, 'node.json')) as stream:
        node = json.load(stream)
    def record(kind, **fields):
        value = dict(identity(), kind=kind, label=LABEL, **fields)
        with open(args.events, 'a') as stream:
            stream.write(json.dumps(value) + '\n')
    record('start')
    if FAIL_STARTUP:
        raise SystemExit(23)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            if (self.path != '/mcp' or
                self.headers.get('Authorization') != 'Bearer ' + node['admin_token']):
                self.send_error(403)
                return
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])).decode('utf-8'))
            call = request['params']['arguments']
            key = (call['realm'], call['operation'])
            if request['method'] != 'tools/call' or request['params']['name'] != 'invoke':
                self.send_error(400)
                return
            if key == ('local', 'system.info') and call['arguments'] == {}:
                result = {'node': node['node'], 'site': node['site'],
                          'release': identity(), 'fixture_label': LABEL}
            elif key == ('site', 'system.discovery') and call['arguments'] == {'section': 'guide'}:
                result = {'section': 'guide', 'items': [{'fixture': True}]}
            else:
                self.send_error(400)
                return
            record('probe', realm=key[0], operation=key[1])
            body = json.dumps({'jsonrpc': '2.0', 'id': request['id'],
                               'result': {'structuredContent': result}}).encode('utf-8')
            try:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # A timed-out readiness request can remain in the backlog.
    server = HTTPServer(('127.0.0.1', 0), Handler, bind_and_activate=False)
    server.socket.close()
    server.socket = socket.socket(fileno=args.socket_fd)
    server.server_address = server.socket.getsockname()
    server.server_name, server.server_port = server.server_address
    try:
        server.serve_forever(poll_interval=.02)
    finally:
        server.server_close()
'''

# The watcher's real restart subprocess talks only to our local fixture manager.
RESTART_HELPER = r'''
import json
import socket
import sys
with socket.create_connection(('127.0.0.1', int(sys.argv[1])), timeout=10) as connection:
    connection.sendall((json.dumps({'nonce': sys.argv[2]}) + '\n').encode('utf-8'))
    with connection.makefile('rb') as stream:
        reply = json.loads(stream.readline().decode('utf-8'))
    if not reply.get('ok'):
        raise SystemExit(1)
'''


def write(path, value):
    with open(path, 'w') as stream:
        stream.write(value)


def save(path, value):
    write(path, json.dumps(value))


def read(path):
    with open(path) as stream:
        return json.load(stream)


def load_watcher():
    path = os.path.join(ROOT, 'deploy', 'controller_autodeploy.py')
    module = types.ModuleType('autodeploy_integration_fixture')
    module.__file__ = path
    with open(path, 'rb') as stream:
        exec(compile(stream.read(), path, 'exec'), module.__dict__)
    return module


@unittest.skipUnless(os.name == 'posix' and shutil.which('git'),
                     'requires POSIX subprocess descriptors and Git')
class WatcherRecoveryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.mkdtemp(prefix='biomerte-autodeploy-e2e-')
        self.addCleanup(shutil.rmtree, self.temporary)
        self.root = os.path.join(self.temporary, 'updater')
        self.home = os.path.join(self.temporary, 'node')
        self.repository = os.path.join(self.temporary, 'source')
        self.remote = os.path.join(self.temporary, 'remote.git')
        self.original = os.path.join(self.temporary, 'original')
        self.events = os.path.join(self.temporary, 'events.jsonl')
        self.config_path = os.path.join(self.temporary, 'launcher.json')
        self.child = None
        self.restarts, self.stopped = [], []
        for path in (self.root, self.home, self.repository):
            os.makedirs(path)
        self.log = open(os.path.join(self.temporary, 'children.log'), 'ab')
        self.addCleanup(self.log.close)
        # Keep the listener open in the parent across restarts. Each launcher
        # inherits only this fd; the fixture controller adopts it after exec.
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(128)
        self.addCleanup(self.listener.close)
        self.addCleanup(self.stop_child)
        token, nonce = uuid.uuid4().hex, uuid.uuid4().hex
        node_path = os.path.join(self.home, 'node.json')
        save(node_path, {'node': 'fixture-node', 'site': 'fixture-site',
                        'admin_token': token, 'port': self.listener.getsockname()[1]})
        with open(node_path, 'rb') as stream:
            self.original_node = stream.read()

        fixture = self
        class RestartHandler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    request = json.loads(self.rfile.readline().decode('utf-8'))
                    if request.get('nonce') != nonce:
                        raise ValueError('fixture authentication failed')
                    fixture.restart_child()
                    reply = {'ok': True}
                except Exception as error:
                    reply = {'ok': False, 'error': type(error).__name__}
                self.wfile.write((json.dumps(reply) + '\n').encode('utf-8'))

        self.manager = socketserver.TCPServer(('127.0.0.1', 0), RestartHandler)
        self.thread = threading.Thread(target=self.manager.serve_forever,
                                       kwargs={'poll_interval': .02})
        self.thread.daemon = True
        self.thread.start()
        self.addCleanup(self.stop_manager)
        helper = os.path.join(self.temporary, 'restart_fixture.py')
        write(helper, RESTART_HELPER)
        self.config = {
            'root': self.root, 'home': self.home, 'repository': self.remote,
            'branch': 'main', 'python': sys.executable,
            'controller_argv': [sys.executable, '-S', '-B', '-m', 'biomerte',
                                '--home', self.home, '--socket-fd',
                                str(self.listener.fileno()), '--events', self.events],
            'restart_commands': [[sys.executable, '-S', helper,
                                  str(self.manager.server_address[1]), nonce]],
            'local_origin': 'http://127.0.0.1:' + str(self.listener.getsockname()[1]),
            'git_timeout': 30, 'command_timeout': 15, 'preflight_timeout': 15,
            'startup_timeout': 4, 'probe_timeout': .2, 'probe_interval': .02}
        save(self.config_path, self.config)
        package = os.path.join(self.repository, 'biomerte')
        os.makedirs(package)
        os.makedirs(os.path.join(self.repository, 'examples'))
        write(os.path.join(package, '__init__.py'), '')
        write(os.path.join(package, 'app.py'), CONTROLLER)
        write(os.path.join(package, '__main__.py'),
              "from .app import run\nif __name__ == '__main__':\n    run()\n")
        shutil.copyfile(os.path.join(ROOT, 'biomerte', 'release.py'),
                        os.path.join(package, 'release.py'))
        write(os.path.join(self.repository, 'examples', 'capability-followup.bpmn'),
              '<definitions />\n')
        self.git('init')
        self.git('checkout', '-b', 'main')
        self.git('init', '--bare', self.remote)
        self.git('remote', 'add', 'origin', self.remote)
        self.good = self.commit('good', False)
        shutil.copytree(package, os.path.join(self.original, 'biomerte'))
        os.symlink(self.original, os.path.join(self.root, 'current'))
        self.watcher_module = load_watcher()
        self.watcher = self.watcher_module.Watcher(self.config)
        self.watcher._restart({'path': self.original, 'commit': None})
        self.watcher._wait_healthy({'path': self.original, 'commit': None})
        self.watcher.initialize(self.original)

    def git(self, *args):
        return subprocess.check_output(['git', '-c', 'user.name=Fixture',
                                        '-c', 'user.email=fixture@example.invalid',
                                        '-c', 'commit.gpgsign=false'] + list(args),
                                       cwd=self.repository, stderr=subprocess.STDOUT)

    def commit(self, label, broken):
        write(os.path.join(self.repository, 'biomerte', 'behavior.py'),
              'FAIL_STARTUP = {0!r}\nLABEL = {1!r}\n'.format(broken, label))
        self.git('add', '.')
        self.git('commit', '-m', label)
        self.git('push', 'origin', 'HEAD:refs/heads/main')
        return self.git('rev-parse', 'HEAD').decode('ascii').strip()

    def stop_child(self):
        if self.child is not None:
            child = self.child
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
            self.stopped.append({'pid': child.pid, 'exit': child.returncode,
                                 'commit': self.restarts[-1]['commit']})
            self.child = None

    def restart_child(self):
        self.stop_child()
        marker = os.path.join(os.path.realpath(os.path.join(self.root, 'current')),
                              '.biomerte-release.json')
        commit = read(marker)['commit'] if os.path.isfile(marker) else None
        self.child = subprocess.Popen([
            sys.executable, '-S', '-B',
            os.path.join(ROOT, 'deploy', 'controller_autodeploy_exec.py'),
            '--config', self.config_path], stdin=subprocess.DEVNULL,
            stdout=self.log, stderr=self.log, pass_fds=(self.listener.fileno(),),
            start_new_session=True)
        self.restarts.append({'commit': commit, 'pid': self.child.pid})

    def stop_manager(self):
        self.manager.shutdown()
        self.thread.join(timeout=5)
        self.manager.server_close()

    def assert_running(self, worker, commit, label):
        self.assertEqual(worker.state['phase'], 'idle', worker.state)
        self.assertIsNone(worker.state['pending'])
        self.assertEqual(worker.state['current']['commit'], commit)
        self.assertEqual(worker.state['last_good']['commit'], commit)
        self.assertEqual(os.path.realpath(worker.current_link),
                         os.path.realpath(worker.state['current']['path']))
        info = worker._health(worker.state['current'])
        self.assertEqual(info['release'], {'commit': commit, 'pid': self.child.pid})
        self.assertEqual(info['fixture_label'], label)
        self.assertIsNone(self.child.poll())
        return info

    def test_real_failed_start_rolls_back_and_new_git_patch_recovers(self):
        self.watcher.tick()
        self.assert_running(self.watcher, self.good, 'good')
        first_pid = self.child.pid
        bad = self.commit('broken-at-startup', True)
        self.watcher.tick()
        self.assert_running(self.watcher, self.good, 'good')
        self.assertNotEqual(self.child.pid, first_pid)
        self.assertEqual(self.watcher.state['rejected_commit'], bad)
        self.assertEqual(self.watcher.state['last_error'], 'authenticated_probe_failed')
        # The bad package passed real imports/staging, actually started, and
        # exited with its intentional startup error before rollback restarted good.
        self.assertTrue(os.path.isfile(os.path.join(self.watcher.releases, bad,
                                                     '.biomerte-release.json')))
        self.assertTrue(any(item['commit'] == bad and item['exit'] == 23
                            for item in self.stopped))
        self.assertEqual([item['commit'] for item in self.restarts],
                         [None, self.good, bad, self.good])

        resumed = self.watcher_module.Watcher(self.config)
        with open(resumed.state_path, 'rb') as stream:
            persisted = stream.read()
        resumed.tick()
        self.assertEqual(len(self.restarts), 4)
        with open(resumed.state_path, 'rb') as stream:
            self.assertEqual(stream.read(), persisted)
        self.assertEqual(resumed.state['rejected_commit'], bad)

        fixed = self.commit('corrective-patch', False)
        resumed.tick()
        self.assert_running(resumed, fixed, 'corrective-patch')
        self.assertIsNone(resumed.state['rejected_commit'])
        self.assertIsNone(resumed.state['last_error'])
        self.assertEqual([item['commit'] for item in self.restarts],
                         [None, self.good, bad, self.good, fixed])
        with open(os.path.join(self.home, 'node.json'), 'rb') as stream:
            self.assertEqual(stream.read(), self.original_node)
        with open(self.events) as stream:
            events = [json.loads(line) for line in stream]
        self.assertTrue(any(item['kind'] == 'start' and item['commit'] == bad
                            for item in events))
        for commit in (self.good, fixed):
            operations = set((item['realm'], item['operation']) for item in events
                             if item['kind'] == 'probe' and item['commit'] == commit)
            self.assertEqual(operations, {('local', 'system.info'),
                                          ('site', 'system.discovery')})
        request = Request(self.config['local_origin'] + '/mcp', data=b'{}',
                          headers={'Authorization': 'Bearer wrong-fixture-token'})
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=2)
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()
        self.assertFalse(any(name.startswith('.prepare-') for name in os.listdir(self.root)))


if __name__ == '__main__':
    unittest.main()
