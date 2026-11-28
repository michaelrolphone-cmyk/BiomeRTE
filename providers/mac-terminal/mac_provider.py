#!/usr/bin/env python3
"""Outbound BiomeRTE transport for enrolled Mac providers."""
import base64
import fcntl
import hashlib
import json
import os
import re
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
ROOT = Path(os.environ['BIOME_MAC_GATEWAY_STATE']) if 'BIOME_MAC_GATEWAY_STATE' in os.environ else Path(__file__).resolve().parent
CREDS = ROOT / 'oauth.json'

class HTTPFailure(RuntimeError):
    def __init__(self, status, body):
        self.status = status
        self.error = body.get('error') if isinstance(body, dict) else None
        # Never echo token response bodies or credentials to persistent logs.
        super().__init__('Gateway HTTP %s (%s)' % (status, body.get('error', 'request rejected') if isinstance(body, dict) else 'non-JSON response'))

class ControllerOperationError(RuntimeError):
    def __init__(self, detail, structured):
        super().__init__(detail[:500])
        status = structured.get('status') if isinstance(structured, dict) else None
        self.status = status if type(status) is int and 400 <= status < 600 else None


class OAuthAuthorizationLost(RuntimeError):
    pass

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
        try:
            c = json.loads(CREDS.read_text())
        except FileNotFoundError as error:
            raise OAuthAuthorizationLost('Legacy OAuth credential is missing') from error
        pending = ROOT / 'refresh-pending.json'
        if pending.exists():
            prior = json.loads(pending.read_text()).get('refresh_sha256')
            current = hashlib.sha256(c['refresh_token'].encode()).hexdigest()
            if prior is None or prior == current:
                raise OAuthAuthorizationLost('Legacy OAuth refresh outcome is uncertain')
            pending.unlink()  # The replacement was already committed before restart.
        if force or time.time() >= c['expires_at']:
            # Rotating refresh tokens must never be blindly replayed after a lost reply.
            save_json(pending, {'started_at': time.time(), 'refresh_sha256': hashlib.sha256(c['refresh_token'].encode()).hexdigest()})
            try:
                token = request(ORIGIN + '/oauth/token', dict(grant_type='refresh_token', client_id=c['client_id'], refresh_token=c['refresh_token'], resource=RESOURCE), form=True)
            except HTTPFailure as error:
                if error.error in ('invalid_grant', 'invalid_client'):
                    raise OAuthAuthorizationLost('Legacy OAuth grant has expired or been revoked') from error
                raise
            c.update(refresh_token=token['refresh_token'], access_token=token['access_token'], expires_at=time.time() + int(token['expires_in']) - 90)
            save_json(CREDS, c)
            pending.unlink()
        return c


def machine_identity(provider):
    path = ROOT / 'machine-providers.json'
    try:
        document = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    entry = document['providers'].get(provider)
    if entry is None:
        return None
    actor = entry['actor']
    if not re.fullmatch('[a-f0-9]{64}', actor):
        raise ValueError('Invalid machine provider key identity')
    key = ROOT / 'machine-keys' / (actor + '.pem')
    if not key.is_file() or key.stat().st_mode & 0o077:
        raise RuntimeError('Machine provider key is missing or not private')
    return document['site'], actor, key


def prepare_machine_key(provider):
    """Generate once per pending enrollment, returning only the public key."""
    keys = ROOT / 'machine-keys'
    keys.mkdir(mode=0o700, exist_ok=True)
    candidate_path = keys / (provider + '.candidate.json')
    with open(keys / 'configuration.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if candidate_path.is_file():
            candidate = json.loads(candidate_path.read_text())
            if (keys / (candidate['actor'] + '.pem')).is_file():
                return candidate
        private = subprocess.check_output(['/usr/bin/openssl', 'genrsa', '2048'], stderr=subprocess.PIPE)
        public = subprocess.check_output(['/usr/bin/openssl', 'pkey', '-pubout'], input=private)
        der = subprocess.check_output(['/usr/bin/openssl', 'pkey', '-pubin', '-outform', 'DER'], input=public)
        actor = hashlib.sha256(der).hexdigest()
        with open(keys / (actor + '.pem'), 'xb', opener=lambda path, flags: os.open(path, flags, 0o600)) as out:
            out.write(private)
            out.flush()
            os.fsync(out.fileno())
        candidate = dict(actor=actor, provider=provider, public_key=public.decode('ascii'))
        save_json(candidate_path, candidate)
        return candidate


def activate_machine_key(provider, actor):
    """Switch the provider only after the candidate proves its live enrollment."""
    if not re.fullmatch('[a-f0-9]{64}', actor):
        raise ValueError('Expected the enrolled machine key fingerprint')
    site = json.loads((ROOT / 'terminal-manifest.json').read_text())['site']
    keys = ROOT / 'machine-keys'
    config_path = ROOT / 'machine-providers.json'
    with open(keys / 'configuration.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            config = json.loads(config_path.read_text())
        except FileNotFoundError:
            config = dict(site=site, providers={})
        if config['site'] != site:
            raise RuntimeError('Machine provider configuration belongs to another site')
        candidate_path = keys / (provider + '.candidate.json')
        existing = config['providers'].get(provider, {})
        if existing.get('actor') != actor and json.loads(candidate_path.read_text())['actor'] != actor:
            raise RuntimeError('Machine key is not the pending provider candidate')
        key = keys / (actor + '.pem')
        if not key.is_file() or key.stat().st_mode & 0o077:
            raise RuntimeError('Machine provider key is missing or not private')
        expected = dict(provider=provider, actor=actor, authentication='machine_key')
        receipt = invoke_machine(provider, 'provider.auth_status', {}, uuid.uuid4().hex, (site, actor, key))
        if receipt != expected:
            raise RuntimeError('Gateway did not confirm the exact machine provider enrollment')
        config['providers'][provider] = dict(actor=actor)
        save_json(config_path, config)
        if candidate_path.is_file() and json.loads(candidate_path.read_text())['actor'] == actor:
            candidate_path.unlink()
        return receipt


def machine_health(provider, state):
    try:
        path = ROOT / ('machine-auth-' + provider + '.json')
        try:
            previous = json.loads(path.read_text())
        except (OSError, ValueError):
            previous = {}
        now = time.time()
        if previous.get('state') != state or now - previous.get('observed_at', 0) >= 30:
            save_json(path, dict(provider=provider, authentication='machine_key', state=state, observed_at=now))
    except OSError:
        # Diagnostic state must never prevent deliberate command recovery.
        pass


def invoke_machine(provider, operation, arguments, rid, identity, controller=None):
    site, actor, key = identity
    envelope = dict(site=site, realm='site', id=rid, operation=operation, arguments=arguments)
    if controller is not None:
        envelope['controller'] = controller
    canonical = json.dumps(envelope, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    signature = subprocess.run(['/usr/bin/openssl', 'dgst', '-sha256', '-sign', str(key)],
                               input=canonical, capture_output=True)
    if signature.returncode:
        machine_health(provider, 'key_unavailable')
        raise RuntimeError('Machine provider could not sign its request')
    envelope['auth'] = dict(actor=actor, signature=base64.b64encode(signature.stdout).decode())
    # Preserve exact actor, envelope and request ID on ambiguous delivery.
    for attempt in range(6):
        try:
            response = request(ORIGIN + '/v1/call', envelope)
            value = response['result']
            machine_health(provider, 'authorized')
            return value
        except HTTPFailure as error:
            if operation == 'stream.attach' or error.status not in (408, 429, 500, 502, 503, 504):
                if error.status == 401 or error.error in (
                        'Machine provider credential revoked or outside its site',
                        'Revoked credential', 'OpenSSL operation failed', 'Unknown peers ' + actor):
                    machine_health(provider, 'authorization_lost')
                detail = error.error if isinstance(error.error, str) else str(error)
                raise ControllerOperationError(detail, {'status': error.status}) from error
        except (ConnectionError, subprocess.TimeoutExpired):
            pass
        if attempt < 5:
            time.sleep(min(2 ** attempt, 15))
    machine_health(provider, 'gateway_unavailable')
    raise ConnectionError('Gateway unavailable; signed provider request identity retained')


def machine_probe(provider):
    identity = machine_identity(provider)
    if identity is None:
        state = 'legacy_oauth'
        if not CREDS.exists() or (ROOT / 'refresh-pending.json').exists():
            state = 'authorization_lost'
        elif json.loads(CREDS.read_text()).get('expires_at', 0) <= time.time():
            state = 'oauth_refresh_due'
        return dict(provider=provider, authentication='oauth', state=state)
    return invoke_machine(provider, 'provider.auth_status', {}, uuid.uuid4().hex, identity)


_persistent = None
_persistent_after = 0

def transport_active():
    return _persistent is not None and _persistent.socket is not None

def invoke(operation, arguments=None, rid=None, realm='site', provider_context=None):
    global _persistent, _persistent_after
    rid = rid or uuid.uuid4().hex
    args = dict(operation=operation, arguments=arguments or {}, request_id=rid, realm=realm)
    if provider_context is not None:
        args['provider_context'] = provider_context
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
                raise ControllerOperationError(detail, result.get('structuredContent'))
            if result.get('structuredContent') is not None:
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

def invoke_provider(operation, arguments=None, rid=None, realm='site', provider=None, controller=None):
    """Report provider locality; the gateway assigns an available controller."""
    if provider is None:
        provider = (arguments or {}).get('provider')
    if not isinstance(provider, str) or not re.fullmatch('[a-f0-9]{64}', provider):
        raise ValueError('Provider identity required for gateway placement')
    try:
        identity = machine_identity(provider)
    except (OSError, ValueError, KeyError, RuntimeError):
        machine_health(provider, 'key_unavailable')
        raise
    if identity is not None:
        if realm != 'site':
            raise ValueError('Machine provider transport requires the site realm')
        return invoke_machine(provider, operation, arguments or {}, rid or uuid.uuid4().hex, identity, controller)
    if controller is not None:
        return invoke('controller.invoke', dict(controller=controller, operation=operation,
                                                arguments=arguments or {}), rid, realm)
    return invoke(operation, arguments, rid, realm,
                  provider_context={'provider': provider, 'host': socket.gethostname()})

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
        result = invoke_provider(name, args, rid, self.realm, self.node)
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
        node_path = ROOT / 'identity/node.json'
        provider_id = json.loads(node_path.read_text())['node'] if node_path.is_file() else None
        if provider_id and machine_identity(provider_id):
            print(json.dumps(machine_probe(provider_id)))
        else:
            if not CREDS.exists():
                authorize()
            configure()
    elif mode == 'authorize':
        authorize()
    elif mode == 'worker':
        worker()
    elif mode == 'verify':
        verify()
    elif mode in ('auth-status', 'prepare-machine-key', 'activate-machine-key'):
        provider_id = sys.argv[2] if len(sys.argv) > 2 else json.loads((ROOT / 'identity/node.json').read_text())['node']
        if not re.fullmatch('[a-f0-9]{64}', provider_id):
            raise SystemExit('Expected a provider identity')
        if mode == 'prepare-machine-key':
            result = prepare_machine_key(provider_id)
        elif mode == 'activate-machine-key':
            if len(sys.argv) != 4:
                raise SystemExit('Expected the provider identity and enrolled key fingerprint')
            result = activate_machine_key(provider_id, sys.argv[3])
        else:
            result = machine_probe(provider_id)
        print(json.dumps(result))
    elif mode == 'probe':
        r = request(ORIGIN + '/oauth/register', dict(client_name='Mac terminal transport probe', redirect_uris=['https://127.0.0.1:17349/biomerte-callback']))
        print('Gateway registration transport: OK' if r.get('client_id') else 'FAILED')
    else:
        raise SystemExit('Expected setup, authorize, worker, verify, auth-status, prepare-machine-key, activate-machine-key, or probe')

