import importlib.util
import json
import queue
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'deploy' / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


watchdog = module('controller_watchdog', 'controller_watchdog.py')
link = module('persistent_link', 'persistent_link.py')


def observation(local=True, site=True, upstream=True, delivery=True):
    return {'at': 1, 'checks': dict(local=local, site=site, upstream=upstream, delivery=delivery), 'errors': {}}


class RecoveryTests(unittest.TestCase):
    def test_three_failed_requests_restart_controller_despite_green_health(self):
        recovery = watchdog.Recovery({})
        actions = []
        failed = observation(local=False)
        failed['checks']['health_endpoint'] = True
        for now in (1, 2):
            self.assertEqual(recovery.observe(failed, actions.append, now)['status'], 'degraded')
        self.assertFalse(actions)
        self.assertEqual(recovery.observe(failed, actions.append, 3)['status'], 'recovering')
        self.assertEqual(actions, ['controller'])
        self.assertEqual(recovery.observe(observation(), actions.append, 4)['status'], 'healthy')

    def test_remote_outage_does_not_restart_host_or_link(self):
        recovery = watchdog.Recovery({})
        actions = []
        for now in range(20):
            recovery.observe(observation(site=False, upstream=False, delivery=False), actions.append, now)
        self.assertEqual(actions, [])
        self.assertEqual(recovery.state['reason'], 'upstream_unavailable')

    def test_delivery_stall_restarts_only_link_and_preserves_backoff_on_watchdog_restart(self):
        recovery = watchdog.Recovery({})
        actions = []
        failed = observation(delivery=False)
        for now in (1, 2, 3):
            recovery.observe(failed, actions.append, now)
        self.assertEqual(actions, ['link'])
        saved = json.loads(json.dumps(recovery.state))
        recovery = watchdog.Recovery({}, saved)
        for now in range(4, 63):
            recovery.observe(failed, actions.append, now)
        self.assertEqual(actions, ['link'])
        recovery.observe(failed, actions.append, 63)
        self.assertEqual(actions, ['link', 'link'])
        self.assertEqual(recovery.state['next_restart']['link'], 183)

    def test_success_resets_failure_streak_and_restart_error_is_not_success(self):
        recovery = watchdog.Recovery({'failure_threshold': 2})
        actions = []
        recovery.observe(observation(local=False), actions.append, 1)
        recovery.observe(observation(), actions.append, 2)
        recovery.observe(observation(local=False), actions.append, 3)
        self.assertFalse(actions)
        def denied(service):
            raise OSError('secret must not be logged')
        state = recovery.observe(observation(local=False), denied, 4)
        self.assertEqual(state['status'], 'degraded')
        self.assertEqual(state['last_action']['outcome'], 'restart_failed')
        self.assertNotIn('secret', json.dumps(state))

    def test_restart_allowlist_never_targets_providers(self):
        with patch.object(watchdog.subprocess, 'run') as run:
            watchdog.restart_service('controller')
            self.assertEqual(run.call_args.args[0][-1], 'gui/{}/org.biomerte.controller'.format(watchdog.os.getuid()))
            self.assertEqual(run.call_count, 2)
            with self.assertRaises(KeyError):
                watchdog.restart_service('terminal')
            self.assertEqual(run.call_count, 2)

    def test_atomic_status_retains_recovery_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'status.json'
            watchdog.atomic_json(path, {'status': 'recovering'})
            watchdog.atomic_json(path, {'status': 'healthy'})
            self.assertEqual(json.loads(path.read_text())['status'], 'healthy')
            self.assertEqual(list(Path(root).iterdir()), [path])

    def test_probes_use_authenticated_read_paths_and_refresh_credentials(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            node = home / 'node.json'
            node.write_text(json.dumps({'node': 'n', 'site': 's', 'admin_token': 'private'}))
            key = home / 'bridge.key'; key.write_text('bridge-private')
            calls = []
            def response(url, payload, token, timeout, access=None):
                calls.append((url, payload, token))
                if url.endswith('/mcp'):
                    args = payload['params']['arguments']
                    value = {'node': 'n', 'realm': 'local'} if args['realm'] == 'local' else {'section': 'guide', 'items': [{}]}
                    return {'result': {'content': [{'type': 'text', 'text': json.dumps(value)}]}}
                if url.endswith('/bridge/ha/status'):
                    return {'site': 's', 'nodes': []}
                return {'content': [{'type': 'text', 'text': json.dumps({'node': 'n', 'health': {'runtime': 'up'}})}]}
            config = dict(watchdog.DEFAULTS, home=root, key=str(key), origin='https://biome.test')
            probes = watchdog.Probes(config, response)
            self.assertTrue(all(probes.collect()['checks'].values()))
            self.assertEqual([c[1]['params']['arguments']['operation'] for c in calls if c[0].endswith('/mcp')], ['system.info', 'system.discovery'])
            self.assertEqual(calls[0][2], 'private')
            node.write_text(json.dumps({'node': 'n', 'site': 's', 'admin_token': 'rotated'}))
            probes.collect(); self.assertEqual(calls[4][2], 'rotated')

    def test_live_socket_without_delivery_falls_back_to_durable_http_inbox(self):
        client = link.ControllerLink.__new__(link.ControllerLink)
        client.jobs = queue.Queue(); client.socket = object()
        client.last_http_poll = 0; client.http_poll_interval = 2
        with patch.object(link.time, 'monotonic', return_value=10):
            self.assertIsNone(client.next())
            self.assertEqual(client.next(), {'idle': True})
        with patch.object(link.time, 'monotonic', return_value=13):
            self.assertIsNone(client.next())
        job = {'id': 'same-command', 'lease': 'same-fence'}
        client.jobs.put(job)
        self.assertIs(client.next(), job)
        client.socket = None
        self.assertIsNone(client.next())

    def test_real_hung_request_recovers_without_stopping_independent_job(self):
        # Disposable server only: GET health stays green while POST stalls.
        server_code = '''import sys,time,json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
class H(BaseHTTPRequestHandler):
 def log_message(self,*a): pass
 def do_GET(self): self.send_response(200);self.end_headers();self.wfile.write(b'{"runtime":"up"}')
 def do_POST(self):
  if sys.argv[1]=='hang': time.sleep(60)
  self.send_response(200);self.end_headers();self.wfile.write(b'{"ok":true}')
s=ThreadingHTTPServer(('127.0.0.1',0),H);print(s.server_port,flush=True);s.serve_forever()
'''
        children = []
        def start(mode):
            child = subprocess.Popen([sys.executable, '-S', '-c', server_code, mode], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            children.append(child)
            return child, 'http://127.0.0.1:' + child.stdout.readline().strip()
        try:
            child, url = start('hang')
            unrelated = subprocess.Popen([sys.executable, '-S', '-c', 'import time;time.sleep(2);print("provider-finished")'], stdout=subprocess.PIPE, text=True)
            children.append(unrelated)
            self.assertEqual(watchdog.request_json(url + '/health')['runtime'], 'up')
            recovery = watchdog.Recovery({'failure_threshold': 2})
            replacement = []
            def restart(service):
                self.assertEqual(service, 'controller')
                child.terminate(); child.wait(5)
                replacement.append(start('ready'))
                self.assertIsNone(unrelated.poll())
            for now in (1, 2):
                with self.assertRaises(watchdog.ProbeError):
                    watchdog.request_json(url + '/mcp', {}, timeout=.1)
                recovery.observe(observation(local=False), restart, now)
            self.assertEqual(len(replacement), 1)
            self.assertTrue(watchdog.request_json(replacement[0][1] + '/mcp', {})['ok'])
            self.assertEqual(recovery.observe(observation(), restart, 3)['status'], 'healthy')
            self.assertEqual(unrelated.communicate(timeout=5)[0].strip(), 'provider-finished')
            self.assertEqual(unrelated.returncode, 0)
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate(); child.wait(5)
                if child.stdout:
                    child.stdout.close()


if __name__ == '__main__':
    unittest.main()
