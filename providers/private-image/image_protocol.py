"""Authenticated ephemeral X25519 sessions. No session state is written to disk."""
import base64
import hashlib
import hmac
import json
import secrets
import uuid
import threading
from cryptography.hazmat.primitives.asymmetric import x25519, ed25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
def b64(value):
    return base64.b64encode(value).decode('ascii')
def raw(text, size):
    value = base64.b64decode(text, validate=True)
    if len(value) != size:
        raise ValueError('Invalid length')
    return value
def ident(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError('Invalid identity')
    return value.encode('ascii')
def public(key):
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
def client_transcript(h):
    return (b'biome-private-image/v2/client-hello\x00' + ident(h['id']) + ident(h['sid']) +
            raw(h['client_identity'], 32) + raw(h['client_ephemeral'], 32) + raw(h['client_nonce'], 32))
def transcript(h, reply, server_identity):
    return (b'biome-private-image/v2/server-hello\x00' + client_transcript(h) +
            raw(reply['server_ephemeral'], 32) + raw(reply['server_nonce'], 32) + server_identity)
def keys(shared, transcript_hash):
    if len(shared) != 32 or shared == bytes(32) or len(transcript_hash) != 32:
        raise ValueError('Invalid shared secret')
    return {kind: HKDF(algorithm=hashes.SHA256(), length=32, salt=transcript_hash,
                       info=('biome-private-image/v2/' + kind).encode()).derive(shared)
            for kind in ('request', 'response')}
def aad(kind, sid, operation, transcript_hash):
    ident(sid); ident(operation)
    if kind not in ('request', 'response', 'progress') or len(transcript_hash) != 32:
        raise ValueError('Invalid binding')
    return ('biome-private-image|2|' + kind + '|' + sid + '|' + operation + '|').encode() + transcript_hash
def seal(key, kind, sid, operation, thash, value):
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(nonce, json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode(), aad(kind, sid, operation, thash))
    return dict(v=2, kind=kind, sid=sid, id=operation, nonce=b64(nonce), ct=b64(ciphertext))
def open_message(key, kind, sid, operation, thash, value):
    if set(value) != {'v','kind','sid','id','nonce','ct'} or value['v'] != 2 or value['kind'] != kind or value['sid'] != sid or value['id'] != operation:
        raise ValueError('Invalid envelope')
    return json.loads(AESGCM(key).decrypt(raw(value['nonce'],12), base64.b64decode(value['ct'],validate=True), aad(kind,sid,operation,thash)))
class SessionServer:
    def __init__(self, identity, enrollment_hash=None, phone_identity=None, enrolled=None):
        self.identity = identity
        self.enrollment_hash = enrollment_hash
        self.phone_identity = phone_identity
        self.enrolled = enrolled or (lambda phone: None)
        self.sessions = {}
    def handle(self, envelope, operation, infer, progress=None):
        if envelope.get('v') != 2 or envelope.get('id') != operation:
            raise ValueError('Invalid operation binding')
        ident(operation)
        if envelope.get('kind') == 'hello':
            return self.hello(envelope)
        if envelope.get('kind') != 'request':
            raise ValueError('Invalid message type')
        sid = envelope['sid']
        session = self.sessions.get(sid)
        if session is None:
            raise ValueError('Session unavailable')
        data = open_message(session['keys']['request'],'request',sid,operation,session['hash'],envelope)
        # Authenticate and validate before removing the one-use session.
        messages = data.get('messages')
        if not isinstance(messages,list) or not messages or any(not isinstance(m,dict) or set(m)!={'role','content'} or m['role'] not in ('system','user','assistant') or (m['role']=='system' and i!=0) or not isinstance(m['content'],str) for i,m in enumerate(messages)):
            raise ValueError('Invalid conversation')
        phone = session['phone']
        if self.phone_identity != phone:
            token = raw(data.get('enrollment_token',''),32)
            if self.enrollment_hash is None or not hmac.compare_digest(hashlib.sha256(token).digest(),self.enrollment_hash):
                raise ValueError('Phone not paired')
            # Atomic caller writes only the public identity and removes the enrollment hash.
            self.enrolled(phone)
            self.phone_identity = phone
            self.enrollment_hash = None
        self.sessions.pop(sid)
        try:
            sequence = 0
            progress_lock = threading.Lock()
            def emit(update):
                nonlocal sequence
                with progress_lock:
                    sequence += 1
                    value = dict(update, seq=sequence)
                    progress(seal(session['keys']['response'],'progress',sid,operation,session['hash'],value))
            answer = infer(messages, emit) if progress is not None else infer(messages)
        except Exception:
            answer = {'error':'private_inference_unavailable'}
        return seal(session['keys']['response'],'response',sid,operation,session['hash'],answer)
    def hello(self, h):
        if set(h) != {'v','kind','id','sid','client_identity','client_ephemeral','client_nonce','signature'}:
            raise ValueError('Invalid hello')
        body = client_transcript(h)
        phone = raw(h['client_identity'],32)
        ed25519.Ed25519PublicKey.from_public_bytes(phone).verify(raw(h['signature'],64),body)
        if phone != self.phone_identity and self.enrollment_hash is None:
            raise ValueError('Phone not paired')
        sid = h['sid']
        prior = self.sessions.get(sid)
        if prior:
            if prior['client'] != body:
                raise ValueError('Session conflict')
            return prior['reply']
        private = x25519.X25519PrivateKey.generate()
        reply = dict(v=2,kind='hello',id=h['id'],sid=sid,
                     server_ephemeral=b64(public(private)),server_nonce=b64(secrets.token_bytes(32)))
        whole = transcript(h,reply,public(self.identity))
        reply['signature'] = b64(self.identity.sign(whole))
        thash = hashlib.sha256(whole).digest()
        traffic = keys(private.exchange(x25519.X25519PublicKey.from_public_bytes(raw(h['client_ephemeral'],32))),thash)
        self.sessions[sid] = dict(keys=traffic,hash=thash,phone=phone,client=body,reply=reply)
        # Neither ephemeral private key nor conversation content is retained.
        return reply

