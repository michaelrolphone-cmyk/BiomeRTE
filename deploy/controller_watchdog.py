#!/usr/bin/env python3
"""Independent Mac controller supervision; no capability execution or replay.

Probes perform authenticated reads. Recovery restarts only the existing
controller or connector launchd service; provider processes and journals remain
untouched. Diagnostic deadlines apply to probes, never to user jobs.
"""
import argparse
import json
import os
import plistlib
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPRedirectHandler

LABELS = {'controller': 'org.biomerte.controller',
          'link': 'org.biomerte.controller-link'}
DEFAULTS = {'interval': 15, 'probe_timeout': 15, 'failure_threshold': 3,
            'restart_cooldown': 60, 'max_restart_cooldown': 300}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ProbeError(Exception):
    def __init__(self, kind):
        self.kind = kind
        super().__init__(kind)


def request_json(url, payload=None, token=None, timeout=15, site_access=None):
    headers = {'Content-Type': 'application/json', 'User-Agent': 'BiomeRTE-Recovery/1'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if site_access:
        headers['OAI-Sites-Authorization'] = 'Bearer ' + site_access
    body = None if payload is None else json.dumps(payload).encode()
    try:
        with build_opener(NoRedirect).open(Request(url, data=body, headers=headers), timeout=timeout) as response:
            return json.load(response)
    except HTTPError as error:
        raise ProbeError('http_' + str(error.code)) from None
    except Exception as error:
        # Never publish URLs, headers, node credentials, or server error bodies.
        raise ProbeError(type(error).__name__) from None


def tool_value(result):
    if not isinstance(result, dict) or result.get('isError'):
        raise ProbeError('tool_error')
    try:
        if isinstance(result.get('structuredContent'), dict):
            return result['structuredContent']
        return json.loads(next(c['text'] for c in result['content'] if c['type'] == 'text'))
    except (KeyError, TypeError, ValueError, StopIteration):
        raise ProbeError('invalid_tool_response') from None


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + '.')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Probes:
    def __init__(self, config, request=request_json):
        self.config, self.request = config, request

    def collect(self):
        config = self.config
        # Re-read credentials on every pass so credential rotation needs no restart.
        conf = json.loads((Path(config['home']) / 'node.json').read_text())
        local = 'http://127.0.0.1:' + str(config.get('port', conf.get('port', 8740)))
        origin = config['origin'].rstrip('/')
        token = Path(config['key']).read_text().strip()
        access = None
        access_file = Path(config['key']).parent / 'site-access.json'
        if access_file.exists():
            saved = json.loads(access_file.read_text())
            if saved.get('origin') != origin:
                raise ProbeError('site_access_origin_mismatch')
            access = saved['token']
        observed = {'at': time.time(), 'checks': {}, 'errors': {}}

        def check(name, action, valid):
            try:
                value = action()
                if not valid(value):
                    raise ProbeError('invalid_probe_result')
                observed['checks'][name] = True
            except Exception as error:
                observed['checks'][name] = False
                observed['errors'][name] = error.kind if isinstance(error, ProbeError) else type(error).__name__

        def invoke(realm, operation, arguments):
            rpc = {'jsonrpc': '2.0', 'id': 'recovery-probe', 'method': 'tools/call',
                   'params': {'name': 'invoke', 'arguments': {
                       'realm': realm, 'operation': operation, 'arguments': arguments}}}
            result = self.request(local + '/mcp', rpc, conf['admin_token'], config['probe_timeout'])
            return tool_value(result.get('result'))

        def remote(path):
            return self.request(origin + path, {}, token,
                                config['probe_timeout'], access)

        check('local', lambda: invoke('local', 'system.info', {}),
              lambda v: v.get('node') == conf['node'] and v.get('realm') == 'local')
        check('site', lambda: invoke('site', 'system.discovery', {'section': 'guide'}),
              lambda v: v.get('section') == 'guide' and isinstance(v.get('items'), list) and bool(v['items']))
        # A gateway/network/journal outage is not a reason to restart the host.
        check('upstream', lambda: remote('/bridge/ha/status'),
              lambda v: v.get('site') == conf['site'] and isinstance(v.get('nodes'), list))
        if observed['checks']['local'] and observed['checks']['site'] and observed['checks']['upstream']:
            check('delivery', lambda: tool_value(remote('/bridge/check')),
                  lambda v: bool(v.get('node')) and v.get('health', {}).get('runtime') == 'up')
        return observed


class Recovery:
    def __init__(self, config, state=None):
        self.config = dict(DEFAULTS, **config)
        self.state = state or {'failures': {}, 'restarts': {}, 'next_restart': {}}
        for key in ('failures', 'restarts', 'next_restart'):
            self.state.setdefault(key, {})

    def observe(self, observed, restart, now=None):
        now = time.time() if now is None else now
        checks = observed['checks']
        reason, target = 'healthy', None
        if not checks.get('local'):
            reason, target = 'local_request_path_failed', 'controller'
        elif not checks.get('upstream'):
            reason = 'upstream_unavailable'
        elif not checks.get('site'):
            reason, target = 'site_request_path_failed', 'controller'
        elif not checks.get('delivery'):
            reason, target = 'gateway_delivery_stalled', 'link'
        self.state.update(observation=observed, reason=reason, status='healthy' if reason == 'healthy' else 'degraded')
        for service in LABELS:
            if service != target:
                self.state['failures'][service] = 0
                # Reset backoff only with a successful observation of that path.
                good = checks.get('local') and checks.get('site') if service == 'controller' else checks.get('delivery')
                if good:
                    self.state['restarts'][service] = 0
            else:
                self.state['failures'][service] = self.state['failures'].get(service, 0) + 1
        if target and self.state['failures'][target] >= self.config['failure_threshold'] and now >= self.state['next_restart'].get(target, 0):
            attempts = self.state['restarts'].get(target, 0)
            delay = min(self.config['max_restart_cooldown'], self.config['restart_cooldown'] * 2 ** min(attempts, 20))
            self.state['restarts'][target] = attempts + 1
            self.state['next_restart'][target] = now + delay
            self.state['failures'][target] = 0
            self.state['last_action'] = {'at': now, 'service': target, 'reason': reason, 'outcome': 'requested'}
            try:
                restart(target)
                self.state['last_action']['outcome'] = 'restart_issued'
                self.state['status'] = 'recovering'
            except Exception as error:
                self.state['last_action']['outcome'] = 'restart_failed'
                self.state['last_action']['error'] = type(error).__name__
        # A restart is never recorded as recovery until the next successful probe.
        return self.state


def restart_service(service):
    label = LABELS[service]
    target = 'gui/{}/{}'.format(os.getuid(), label)
    # A deliberately unloaded service stays unloaded. No arbitrary executables,
    # scripts, capability calls, job termination, or journal edits are accepted.
    subprocess.run(['/bin/launchctl', 'print', target], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    subprocess.run(['/bin/launchctl', 'kickstart', '-k', target], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)


def install(home, origin, key, python):
    directory = Path(home) / 'recovery'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    script = directory / 'controller_watchdog.py'
    if Path(__file__).resolve() != script.resolve():
        script.write_bytes(Path(__file__).read_bytes())
    config = dict(DEFAULTS, home=str(home), origin=origin, key=str(key), enabled=True)
    config_path = directory / 'config.json'
    if not config_path.exists():
        atomic_json(config_path, config)
    label = 'org.biomerte.controller-watchdog'
    path = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
    data = {'Label': label, 'ProgramArguments': [python, '-S', str(script), '--config', str(config_path)],
            'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 3,
            'StandardOutPath': str(directory / 'watchdog.log'),
            'StandardErrorPath': str(directory / 'watchdog-error.log')}
    if path.exists() and path.read_bytes() != plistlib.dumps(data):
        path.with_suffix('.plist.before-' + str(time.time_ns())).write_bytes(path.read_bytes())
    path.write_bytes(plistlib.dumps(data)); path.chmod(0o600)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--once', action='store_true')
    options = parser.parse_args()
    if not options.config:
        parser.error('--config required')
    state_path = options.config.parent / 'status.json'
    try:
        state = json.loads(state_path.read_text())
    except (FileNotFoundError, ValueError):
        state = None
    recovery = None
    while True:
        config = dict(DEFAULTS, **json.loads(options.config.read_text()))
        recovery = Recovery(config, state)
        if not config.get('enabled', True) or (options.config.parent / 'paused').exists():
            state = dict(recovery.state, status='paused', observed_at=time.time())
        else:
            try:
                state = recovery.observe(Probes(config).collect(), restart_service)
            except Exception as error:
                state = dict(recovery.state, status='probe_error', observed_at=time.time(),
                             error=error.kind if isinstance(error, ProbeError) else type(error).__name__)
        atomic_json(state_path, state)
        if options.once:
            print(json.dumps(state)); return
        time.sleep(config['interval'])


if __name__ == '__main__':
    main()
