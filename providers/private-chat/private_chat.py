#!/usr/bin/env python3
"""Isolated encrypted chat adapter. Only encrypted results reach the worker."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import resource
import secrets
import subprocess
import sys
import time
from urllib.request import Request, urlopen, build_opener, ProxyHandler, HTTPRedirectHandler
import keyring
from crypto_wire import seal, open_envelope, b64, unb64

HOME = Path.home()
STATE = HOME / '.local/share/biome-private-chat'
TERMINAL = HOME / '.local/share/biome-mac-terminal'
LM = HOME / '.local/share/biome-lmstudio'
sys.path[:0] = [str(TERMINAL), str(TERMINAL / 'runtime')]
import mac_provider as gateway
from biomerte.worker import Worker
from biomerte.store import digest, encode
SERVICE = 'org.biomerte.private-chat'
ACCOUNT = 'transport-v1'
CONTRACT = 'chat.private.exchange:1'
LABEL = 'org.biomerte.private-chat'
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError('Local redirect refused')
LOCAL = build_opener(ProxyHandler({}), NoRedirect())
def transport_key():
    # Refuse file-backed or fallback keyring backends.
    if not keyring.get_keyring().__class__.__module__.startswith('keyring.backends.macOS'):
        raise RuntimeError('macOS Keychain is required')
    value = keyring.get_password(SERVICE, ACCOUNT)
    if not value:
        raise RuntimeError('Pairing is unavailable')
    return unb64(value)
def privacy_ready():
    """An operator must validate LM Studio logging before enabling private inference."""
    cfg = json.loads((STATE / 'privacy.json').read_text())
    if cfg != {'lmstudio_request_logging_disabled': True, 'qualification': 'verified'}:
        raise RuntimeError('LM Studio privacy qualification required')
    server = json.loads((HOME / '.lmstudio/.internal/http-server-config.json').read_text())
    if server.get('logSensitiveData') is not False or server.get('logIncomingTokens') is not False:
        raise RuntimeError('LM Studio sensitive logging must be disabled')
def inference(messages):
    privacy_ready()
    if not isinstance(messages, list) or not messages:
        raise ValueError('Invalid conversation')
    if any(not isinstance(m, dict) or set(m) != {'role', 'content'} or m['role'] not in ('user', 'assistant') or not isinstance(m['content'], str) for m in messages):
        raise ValueError('Invalid message')
    # Stateless OpenAI-compatible API; no response IDs or coding integrations.
    body = dict(model='qwen3.5-9b', messages=messages, stream=False, store=False)
    headers = {'Content-Type': 'application/json'}
    token_file = LM / 'api-token'
    if token_file.exists():
        headers['Authorization'] = 'Bearer ' + token_file.read_text().strip()
    req = Request('http://127.0.0.1:1234/v1/chat/completions', json.dumps(body).encode(), headers)
    with LOCAL.open(req) as response:
        result = json.load(response)
    if 'response_id' in result:
        raise RuntimeError('Unexpected retained response')
    choice = result['choices'][0]
    if choice.get('finish_reason') not in ('stop', None):
        raise RuntimeError('Incomplete completion')
    text = choice['message']['content']
    if not isinstance(text, str) or not text:
        raise RuntimeError('Empty completion')
    return {'text': text}
def execute(request):
    master = transport_key()
    envelope = request['inputs']['envelope']
    # Do not echo unauthenticated data or exception text.
    data = open_envelope(master, 'request', envelope)
    if envelope['id'] != request['operation']:
        raise ValueError('Identity mismatch')
    try:
        answer = inference(data['messages'])
    except Exception:
        answer = {'error': 'private_inference_unavailable'}
    return {'envelope': seal(master, 'response', envelope['id'], answer)}
class Remote:
    realm = 'site'
    def __init__(self, cfg):
        self.node, self.site = cfg['provider'], cfg['site']
    def call(self, name, args, rid=None):
        return gateway.invoke(name, args, rid, self.realm)
class PrivateWorker(Worker):
    """Disk ledger retains hashes and state only; response retries live in RAM."""
    def __init__(self, *args):
        super().__init__(*args)
        self.pending = {}
    def perform(self, action):
        args = dict(provider=self.remote.node, operation=action['id'], generation=action['generation'])
        guard = action.get('guard', action['resource'])
        binding = self.db.execute('SELECT gen,holder FROM fences WHERE resource=?', (guard,)).fetchone()
        if binding != (action['generation'], action['lease']):
            self.fence(dict(action, id=action['lease'], guard=guard))
        claimed = self.remote.call('provider.claim', args)
        if claimed['state'] != 'claimed':
            self.pending.pop(action['id'], None)
            self.db.execute("UPDATE actions SET state=?,result='{}' WHERE id=?", (claimed['state'], action['id']))
            return
        signature = digest([action['contract'], action['inputs'], action['resource'], action['generation'], action['lease']])
        row = self.db.execute('SELECT signature,state FROM actions WHERE id=?', (action['id'],)).fetchone()
        if row and row[0] != signature:
            raise ValueError('Operation conflict')
        if action['id'] not in self.pending:
            if row:
                # A crash must not reconstruct history or silently replay inference.
                state, out = 'failed', {'reason': 'interrupted_private_request_send_new_request'}
            else:
                self.db.execute("INSERT INTO actions VALUES(?,?,?,?,?)", (action['id'], signature, 'uncertain', '{}', guard))
                adapter = next(o for o in self.offers if o['contract'] == action['contract'])
                proc = subprocess.run(adapter['argv'], input=encode({'operation':action['id'],'inputs':action['inputs']}).encode(),
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                if proc.returncode:
                    state, out = 'failed', {'reason': 'private_adapter_unavailable'}
                else:
                    state, out = 'succeeded', json.loads(proc.stdout)
            self.pending[action['id']] = (state, out)
        state, out = self.pending[action['id']]
        self.remote.call('provider.result', dict(args, state=state, result=out))
        self.db.execute("UPDATE actions SET state=?,result='{}' WHERE id=?", (state, action['id']))
        self.pending.pop(action['id'], None)

def serve():
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    cfg = json.loads((STATE / 'manifest.json').read_text())
    remote = Remote(cfg)
    offer = dict(contract=CONTRACT, resource=cfg['resource'], capacity=1, service='private-chat',
                 argv=[sys.executable, str(Path(__file__).resolve()), 'execute'])
    with open(STATE / 'worker.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        worker = PrivateWorker(str(STATE / 'worker.sqlite'), remote, [offer], None)
        while True:
            try:
                privacy_ready()
                worker.advertise()
                break
            except Exception:
                time.sleep(5)
        while True:
            try:
                worker.once()
            except Exception:
                # No exception details, request bodies or answers in logs.
                pass
            time.sleep(2)
def install():
    os.umask(0o077)
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    cfg_path = STATE / 'manifest.json'
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text())
        transport_key()
    else:
        if not keyring.get_keyring().__class__.__module__.startswith('keyring.backends.macOS'):
            raise RuntimeError('macOS Keychain is required')
        master = secrets.token_bytes(32)
        try:
            keyring.set_password(SERVICE, ACCOUNT, b64(master))
        except keyring.errors.PasswordSetError:
            raise SystemExit('Mac login Keychain refused key storage. Unlock it locally, then rerun installation; no pairing key was saved.')
        site = json.loads((TERMINAL / 'terminal-manifest.json').read_text())['site']
        provider = hashlib.sha256(('private-chat/' + secrets.token_hex(32)).encode()).hexdigest()
        cfg = dict(provider=provider, resource='privatechat.mac.' + provider[:20], site=site)
        gateway.save_json(cfg_path, cfg)
    gateway.invoke('cap.define', dict(name='chat.private.exchange', version=1,
        description='Opaque authenticated ciphertext for private LM Studio conversations; never send plaintext.',
        inputs={'envelope': 'obj'}, outputs={'envelope': 'obj'}))
    # The pairing secret stays on the Mac; never print it or put it in Biome.
    pairing = STATE / 'pairing.json'
    gateway.save_json(pairing, dict(v=1, key=b64(transport_key()), resource=cfg['resource']))
    spec = dict(Label=LABEL, ProgramArguments=[sys.executable, str(Path(__file__).resolve()), 'serve'],
                RunAtLoad=True, KeepAlive=True, WorkingDirectory=str(STATE),
                StandardOutPath='/dev/null', StandardErrorPath='/dev/null',
                EnvironmentVariables={'HOME': str(HOME), 'PATH': os.environ['PATH']})
    plist = HOME / 'Library/LaunchAgents' / (LABEL + '.plist')
    plist.write_bytes(plistlib.dumps(spec))
    print('Installed private bridge. Pairing file:', pairing)
    print('Inference remains disabled until LM Studio logging is qualified.')
if __name__ == '__main__':
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if sys.argv[1] == 'install':
        install()
    elif sys.argv[1] == 'serve':
        serve()
    elif sys.argv[1] == 'execute':
        try:
            json.dump(execute(json.load(sys.stdin)), sys.stdout)
        except Exception:
            sys.exit(1)
