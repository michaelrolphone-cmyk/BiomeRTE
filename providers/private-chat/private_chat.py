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
from session_protocol import SessionServer, b64, raw, public, conversation_messages
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption

HOME = Path.home()
STATE = Path(os.environ.get('BIOME_PRIVATE_CHAT_STATE', HOME / '.local/share/biome-private-chat'))
TERMINAL = Path(os.environ.get('BIOME_GATEWAY_CODE', HOME / '.local/share/biome-mac-terminal'))
LM = HOME / '.local/share/biome-lmstudio'
sys.path[:0] = [str(TERMINAL), str(TERMINAL / 'runtime')]
import mac_provider as gateway
from biomerte.worker import Worker
from biomerte.store import digest, encode
SERVICE = 'org.biomerte.private-chat'
CONTRACT = 'chat.private.exchange:2'
LABEL = 'org.biomerte.private-chat'
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError('Local redirect refused')
LOCAL = build_opener(ProxyHandler({}), NoRedirect())
def identity_key():
    path = STATE / 'identity.ed25519'
    stat = path.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise RuntimeError('Identity file must be owner-only')
    value = path.read_bytes()
    if len(value) != 32:
        raise RuntimeError('Invalid identity')
    return Ed25519PrivateKey.from_private_bytes(value)
from model_service import ModelError, NativeChatBackend, model_catalog, selected_model
_BACKEND = None

def model_backend():
    global _BACKEND
    if _BACKEND is None:
        config = os.environ.get('BIOME_MODEL_HOST_IO_CONFIG')
        if config:
            from biomerte.host_io_client import HostIOClient
            from biomerte.model_host_io import ModelHostClient
            _BACKEND = ModelHostClient(HostIOClient.from_config(config))
        else:
            if sys.platform != 'darwin':
                raise RuntimeError('Private workers require the native Mac model bridge')
            _BACKEND = NativeChatBackend(state=STATE, lm=LM)
    return _BACKEND

def privacy_ready():
    return model_backend().privacy_ready()

def handle_request(data):
    privacy_ready()
    if data.get('action') == 'models':
        return model_backend().catalog()
    if data.get('action') not in (None, ''):
        raise ValueError('Invalid private action')
    return inference(data['messages'], data.get('model'))

def inference(messages, model=None):
    return model_backend().inference(messages, model)

class Remote:
    realm = 'site'
    def __init__(self, cfg):
        self.node, self.site = cfg['provider'], cfg['site']
    def call(self, name, args, rid=None):
        result = gateway.invoke_provider(name, args, rid, self.realm, self.node)
        health = os.environ.get('BIOME_WORKER_HEALTH')
        if health:
            try:
                # Successful encrypted progress/artifact delivery also proves
                # this worker is making progress between provider polls.
                gateway.save_json(Path(health), {'last_contact':time.time(), 'state':'online'})
            except OSError:
                pass
        return result
class PrivateWorker(Worker):
    """Disk ledger retains hashes and state only; response retries live in RAM."""
    def advertise(self):
        # The generic legacy worker forwards only capacity/usage. Preserve the
        # private service's explicitly configured reservation lifecycle too.
        for offer in self.offers:
            args = {key: offer[key] for key in ('contract', 'resource', 'capacity', 'service', 'configuration', 'usage') if key in offer}
            args.update(provider=self.remote.node, state=offer.get('state', 'ready'))
            self.remote.call('cap.offer', args)

    def __init__(self, *args, handler=None):
        super().__init__(*args)
        self.pending = {}
        self.handler = handler
    def reconcile(self):
        # Recover metadata after a lost provider.result reply or process restart.
        for operation,signature in self.db.execute("SELECT id,signature FROM actions WHERE state='uncertain'").fetchall():
            action = self.remote.call('cap.status',{'operation':operation})
            expected = digest([action['contract'],action['inputs'],action['resource'],action['generation'],action['lease']])
            if action['provider'] != self.remote.node or signature != expected:
                raise ValueError('Reconciliation identity mismatch')
            if action['state'] in ('succeeded','failed'):
                state = action['state']
            elif action['state'] in ('claimed','uncertain'):
                state,out = self.pending.get(operation,('failed',{'reason':'interrupted_private_request_send_new_request'}))
                self.remote.call('provider.result',dict(provider=self.remote.node,operation=operation,
                                 generation=action['generation'],state=state,result=out))
            else:
                raise ValueError('Unexpected interrupted state')
            self.db.execute("UPDATE actions SET state=?,result='{}' WHERE id=?",(state,operation))
            self.pending.pop(operation,None)
    def once(self, pool=None):
        self.reconcile()
        return super().once(pool)
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
                try:
                    out = self.handler(action)
                    state = 'succeeded'
                except Exception:
                    state, out = 'failed', {'reason': 'private_request_unavailable'}
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
        configuration={'reservation_policy': cfg.get('reservation_policy',
            {'idle_seconds': 120, 'release_after_result': True})})
    def enrolled(phone):
        cfg['phone_identity'] = b64(phone)
        cfg.pop('enrollment_hash', None)
        gateway.save_json(STATE / 'manifest.json', cfg)
        (STATE / 'pairing.json').unlink(missing_ok=True)
    protocol = SessionServer(identity_key(),
        raw(cfg['enrollment_hash'],32) if cfg.get('enrollment_hash') else None,
        raw(cfg['phone_identity'],32) if cfg.get('phone_identity') else None, enrolled, request_handler=handle_request)
    def dispatch(action):
        # Image and chat providers share the current paired phone public identity.
        current = json.loads((STATE / 'manifest.json').read_text())
        protocol.phone_identity = raw(current['phone_identity'],32) if current.get('phone_identity') else None
        protocol.enrollment_hash = raw(current['enrollment_hash'],32) if current.get('enrollment_hash') else None
        return {'envelope': protocol.handle(action['inputs']['envelope'], action['id'], inference)}
    with open(STATE / 'worker.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        worker = PrivateWorker(str(STATE / 'worker-v2.sqlite'), remote, [offer], None,
            handler=dispatch)
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
            time.sleep(0 if getattr(gateway, 'transport_active', lambda: False)() else 2)
def install():
    os.umask(0o077)
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    cfg_path = STATE / 'manifest.json'
    cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    site = json.loads((TERMINAL / 'terminal-manifest.json').read_text())['site']
    if cfg.get('site',site) != site:
        raise RuntimeError('Existing provider belongs to another site')
    identity_path = STATE / 'identity.ed25519'
    if not identity_path.exists():
        private = Ed25519PrivateKey.generate().private_bytes(Encoding.Raw,PrivateFormat.Raw,NoEncryption())
        fd = os.open(identity_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as out:
            out.write(private); out.flush(); os.fsync(out.fileno())
    identity = identity_key()
    if '--reset-pairing' in sys.argv:
        cfg.pop('phone_identity', None)
    provider = cfg.get('provider') or hashlib.sha256(('private-chat/' + secrets.token_hex(32)).encode()).hexdigest()
    cfg.update(v=2,provider=provider,resource=cfg.get('resource','privatechat.mac.'+provider[:20]),site=site)
    pairing = dict(v=2,server_identity=b64(public(identity)),resource=cfg['resource'])
    if not cfg.get('phone_identity'):
        token = secrets.token_bytes(32)
        cfg['enrollment_hash'] = b64(hashlib.sha256(token).digest())
        pairing['enrollment_token'] = b64(token)
    gateway.save_json(cfg_path,cfg)
    gateway.invoke('cap.define',dict(name='chat.private.exchange',version=2,
        description='Authenticated ephemeral X25519 sessions; no chat key persists on Mac.',
        inputs={'envelope':'obj'},outputs={'envelope':'obj'}))
    gateway.save_json(STATE/'pairing.json',pairing)
    spec = dict(Label=LABEL,ProgramArguments=[sys.executable,str(Path(__file__).resolve()),'serve'],
                RunAtLoad=True,KeepAlive=True,WorkingDirectory=str(STATE),
                StandardOutPath='/dev/null',StandardErrorPath='/dev/null',
                EnvironmentVariables={'HOME':str(HOME),'PATH':os.environ['PATH']})
    plist = HOME/'Library/LaunchAgents'/(LABEL+'.plist')
    plist.write_bytes(plistlib.dumps(spec))
    print('Installed protocol v2. Pairing file:',STATE/'pairing.json')
    print('Only the signing identity persists; conversation keys are RAM-only.')
if __name__ == '__main__':
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if sys.argv[1] == 'install':
        install()
    elif sys.argv[1] == 'serve':
        serve()

