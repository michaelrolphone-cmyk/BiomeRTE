#!/usr/bin/env python3
"""Outbound OAuth MCP transport for the existing BiomeRTE durable worker."""
import base64
import fcntl
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid
from pathlib import Path

ORIGIN = 'https://biomerte-controller.cantikash.chatgpt.site'
RESOURCE = ORIGIN + '/api/mcp'
ROOT = Path(__file__).resolve().parent
CREDS = ROOT / 'oauth.json'

class HTTPFailure(RuntimeError):
    def __init__(self, status, body):
        self.status = status
        # Never echo token response bodies or credentials to persistent logs.
        super().__init__('Gateway HTTP %s (%s)' % (status, body.get('error', 'request rejected') if isinstance(body, dict) else 'non-JSON response'))

def request(url, obj=None, form=False, headers=None):
    if urllib.parse.urlsplit(url).scheme != 'https' or urllib.parse.urlsplit(url).netloc != urllib.parse.urlsplit(ORIGIN).netloc:
        raise RuntimeError('Unexpected gateway origin')
    h = {'Accept': 'application/json', 'User-Agent': 'BiomeRTE-Mac-Terminal/4.0', **(headers or {})}
    data = None
    if obj is not None:
        data = (urllib.parse.urlencode(obj) if form else json.dumps(obj)).encode()
        h['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
    # Native curl avoids the gateway rejection seen with Python urllib.
    # Secrets go through stdin and a mode-0600 config, never process arguments.
    with tempfile.TemporaryDirectory(prefix='biome-http-') as folder:
        cfg = Path(folder) / 'curl.conf'
        def quoted(s):
            if '\r' in s or '\n' in s:
                raise ValueError('Invalid HTTP header')
            return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'
        cfg.write_text('\n'.join('header = ' + quoted(k + ': ' + v) for k, v in h.items()))
        cfg.chmod(0o600)
        argv = ['/usr/bin/curl', '--silent', '--show-error', '--proto', '=https', '--connect-timeout', '30', '--max-time', '90', '--config', str(cfg), '--write-out', '\n%{http_code}', url]
        if data is not None:
            argv.extend(['--data-binary', '@-'])
        p = subprocess.run(argv, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=100)
        if p.returncode:
            raise ConnectionError('Gateway transport failed (curl %d)' % p.returncode)
        raw, status = p.stdout.rsplit(b'\n', 1)
        try:
            value = json.loads(raw)
        except ValueError:
            value = None
        if not 200 <= int(status) < 300:
            raise HTTPFailure(int(status), value)
        if value is None:
            raise RuntimeError('Gateway returned a non-JSON response')
        return value

def save_json(path, value):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + '.')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def authorize():
    redirect = 'https://127.0.0.1:17349/biomerte-callback'
    reg = request(ORIGIN + '/oauth/register', {'client_name': 'Mac mini terminal provider', 'redirect_uris': [redirect], 'token_endpoint_auth_method': 'none', 'grant_types': ['authorization_code', 'refresh_token'], 'response_types': ['code']})
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    state = secrets.token_urlsafe(24)
    params = dict(client_id=reg['client_id'], response_type='code', redirect_uri=redirect, code_challenge=challenge, code_challenge_method='S256', state=state, scope='biomerte offline_access', resource=RESOURCE)
    url = ORIGIN + '/oauth/authorize?' + urllib.parse.urlencode(params)
    print('\nSign in to BiomeRTE using this URL:\n\n' + url, flush=True)
    subprocess.run(['/usr/bin/open', url], check=False)
    print('\nAfter allowing access, RIGHT-CLICK “Continue to your app” and copy its link address.\nPaste that link below; do not open it. No local callback server is needed.', flush=True)
    callback = input('Callback URL: ').strip()
    parsed = urllib.parse.urlsplit(callback)
    if (parsed.scheme, parsed.netloc, parsed.path, parsed.fragment) != ('https', '127.0.0.1:17349', '/biomerte-callback', ''):
        raise RuntimeError('Unexpected callback URL')
    q = urllib.parse.parse_qs(parsed.query)
    if q.get('state') != [state] or q.get('iss') != [ORIGIN] or len(q.get('code', [])) != 1 or 'error' in q:
        raise RuntimeError('OAuth callback was denied or failed validation')
    token = request(ORIGIN + '/oauth/token', dict(grant_type='authorization_code', client_id=reg['client_id'], redirect_uri=redirect, code=q['code'][0], code_verifier=verifier, resource=RESOURCE), form=True)
    save_json(CREDS, dict(client_id=reg['client_id'], refresh_token=token['refresh_token'], access_token=token['access_token'], expires_at=time.time() + int(token['expires_in']) - 90))
    (ROOT / 'refresh-pending.json').unlink(missing_ok=True)

def credentials(force=False):
    with open(ROOT / 'oauth.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        c = json.loads(CREDS.read_text())
        if (ROOT / 'refresh-pending.json').exists():
            raise RuntimeError('A refresh reply was lost. Re-run installer with --reauthorize to restore access without replaying a spent token.')
        if force or time.time() >= c['expires_at']:
            # Rotating refresh tokens must never be blindly replayed after a lost reply.
            save_json(ROOT / 'refresh-pending.json', {'started_at': time.time()})
            token = request(ORIGIN + '/oauth/token', dict(grant_type='refresh_token', client_id=c['client_id'], refresh_token=c['refresh_token'], resource=RESOURCE), form=True)
            c.update(refresh_token=token['refresh_token'], access_token=token['access_token'], expires_at=time.time() + int(token['expires_in']) - 90)
            save_json(CREDS, c)
            (ROOT / 'refresh-pending.json').unlink()
        return c

_persistent = None
_persistent_after = 0

def transport_active():
    return _persistent is not None and _persistent.socket is not None

def invoke(operation, arguments=None, rid=None, realm='site'):
    global _persistent, _persistent_after
    rid = rid or uuid.uuid4().hex
    args = dict(operation=operation, arguments=arguments or {}, request_id=rid, realm=realm)
    rpc = dict(jsonrpc='2.0', id=rid, method='tools/call', params=dict(name='biome_invoke', arguments=args))
    # Keep the same identity across transport failures and delayed controller replies.
    for attempt in range(6):
        try:
            c = credentials()
            res = None
            if time.monotonic() >= _persistent_after:
                try:
                    if _persistent is None:
                        from persistent_transport import PersistentTransport
                        _persistent = PersistentTransport(ORIGIN, credentials)
                    res = _persistent.invoke(args)
                except Exception:
                    # Reconcile/retry the identical durable request over HTTP.
                    # A connection failure never creates another command ID.
                    _persistent_after = time.monotonic() + 10
            if res is None:
                res = request(RESOURCE, rpc, headers={'Authorization': 'Bearer ' + c['access_token']})
            if 'error' in res:
                raise RuntimeError('MCP protocol error: ' + str(res['error'].get('code')))
            result = res['result']
            if result.get('isError'):
                detail = ' '.join(x.get('text', '') for x in result.get('content', []) if x.get('type') == 'text')
                if 'has not replied yet' in detail or 'queue is busy' in detail:
                    raise ConnectionError('Controller reply pending')
                raise RuntimeError(detail[:500])
            if 'structuredContent' in result:
                return result['structuredContent']
            return json.loads(next(x['text'] for x in result['content'] if x['type'] == 'text'))
        except HTTPFailure as exc:
            if exc.status == 401 and attempt == 0:
                credentials(force=True)
            elif exc.status not in (408, 429, 500, 502, 503, 504):
                raise
        except (ConnectionError, subprocess.TimeoutExpired):
            pass
        if attempt < 5:
            time.sleep(min(2 ** attempt, 15))
    raise ConnectionError('Gateway unavailable; request identity retained by worker')

def configure():
    info = invoke('system.info')
    nodefile = ROOT / 'identity' / 'node.json'
    if nodefile.exists():
        node = json.loads(nodefile.read_text())['node']
    else:
        nodefile.parent.mkdir(mode=0o700, exist_ok=True)
        node = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        save_json(nodefile, {'node': node})
    manifest_path = ROOT / 'terminal-manifest.json'
    prior = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    # Preserve the old resource and durable authority when upgrading v1/v2.
    resource = prior.get('offers', [{}])[0].get('resource') if prior.get('offers') else None
    resource = resource or 'terminal.mac.' + node[:20]
    if prior.get('site') and prior['site'] != info['site']:
        raise RuntimeError('Controller site changed; existing identity/ledger preserved for reconciliation')
    from terminal import terminal_pool
    executable = os.path.realpath('/bin/bash')
    profile = dict(executable=executable, executable_sha256=hashlib.sha256(Path(executable).read_bytes()).hexdigest(),
                   limits={})
    manifest = terminal_pool(resource, profile, [sys.executable, str(ROOT / 'terminal_adapter.py')])
    for offer in manifest['offers']:
        offer['acknowledge_argv'] = [sys.executable, str(ROOT / 'terminal_adapter.py'), '--acknowledge']
    manifest.update(site=info['site'], realm='site', poll_seconds=2, retry_seconds=5,
                    timeout=None, terminal_profile=profile)
    save_json(manifest_path, manifest)
    print('Provider:', node, '\nResource:', resource, '\nHost:', socket.gethostname(), flush=True)

class Remote:
    realm = 'site'

    def __init__(self, manifest):
        self.site = manifest['site']
        self.node = json.loads((ROOT / 'identity/node.json').read_text())['node']
        self.resource = manifest['offers'][0]['resource']
        self.last_health = 0

    def call(self, name, args, rid=None):
        result = invoke(name, args, rid, self.realm)
        if name == 'provider.poll' and time.monotonic() - self.last_health >= 10:
            save_json(ROOT / 'health.json', dict(provider=self.node, resource=self.resource,
                                                 last_poll=time.time(), state='online'))
            self.last_health = time.monotonic()
        return result


def worker():
    sys.path.insert(0, str(ROOT / 'runtime'))
    from biomerte.worker import run_worker
    manifest = json.loads((ROOT / 'terminal-manifest.json').read_text())
    run_worker(str(ROOT), Remote(manifest), manifest, False)


def verify():
    sys.path.insert(0, str(ROOT / 'runtime'))
    from biomerte.worker import Worker
    manifest = json.loads((ROOT / 'terminal-manifest.json').read_text())
    with open(ROOT / 'worker.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        w = Worker(str(ROOT / 'worker.sqlite'), Remote(manifest), manifest['offers'], manifest['timeout'])
        w.advertise()
        lease = invoke('cap.acquire', dict(contract='host.terminal.run:1', resource=manifest['offers'][0]['resource'], task='mac-installer-check-' + uuid.uuid4().hex))
        try:
            deadline = time.monotonic() + 180
            while lease['state'] != 'bound':
                if time.monotonic() > deadline:
                    raise RuntimeError('Terminal lease not bound before setup timeout')
                w.once()
                lease = invoke('cap.lease', {'lease': lease['id']})
                time.sleep(.5)
            operation = 'mac-installer-check-' + uuid.uuid4().hex
            invoke('cap.invoke', dict(lease=lease['id'], operation=operation, inputs={'argv': ['/bin/bash', '-c', 'printf "BiomeRTE Mac terminal OK\\n"; /usr/bin/uname -s; /bin/hostname']}))
            while True:
                w.once()
                result = invoke('cap.status', {'operation': operation})
                if result['state'] == 'succeeded':
                    if result['result']['exit_code'] != 0 or 'Darwin' not in result['result']['stdout']:
                        raise RuntimeError('Mac terminal self-test returned an unexpected result')
                    print(result['result']['stdout'], flush=True)
                    break
                if result['state'] in ('failed', 'uncertain') or time.monotonic() > deadline:
                    raise RuntimeError('Terminal self-test requires reconciliation: ' + operation)
                time.sleep(.5)
        finally:
            invoke('cap.release', {'lease': lease['id']})
            w.db.close()

if __name__ == '__main__':
    os.umask(0o077)
    mode = sys.argv[1] if len(sys.argv) > 1 else ''
    if mode == 'setup':
        if not CREDS.exists():
            authorize()
        configure()
    elif mode == 'authorize':
        authorize()
    elif mode == 'worker':
        worker()
    elif mode == 'verify':
        verify()
    elif mode == 'probe':
        r = request(ORIGIN + '/oauth/register', dict(client_name='Mac terminal transport probe', redirect_uris=['https://127.0.0.1:17349/biomerte-callback']))
        print('Gateway registration transport: OK' if r.get('client_id') else 'FAILED')
    else:
        raise SystemExit('Expected setup, authorize, worker, or probe')

