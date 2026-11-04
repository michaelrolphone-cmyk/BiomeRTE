"""Interoperable authenticated envelopes; no plaintext persistence."""
import base64
import hashlib
import hmac
import json
import secrets
import uuid
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
def b64(data):
    return base64.b64encode(data).decode('ascii')
def unb64(text):
    return base64.b64decode(text, validate=True)
def direction(master, kind):
    if len(master) != 32 or kind not in ('request', 'response'):
        raise ValueError('Invalid key')
    return hmac.new(master, ('Biome Private Chat v1 ' + kind).encode(), hashlib.sha256).digest()
def aad(kind, identity):
    if str(uuid.UUID(identity)) != identity:
        raise ValueError('Invalid request identity')
    return ('biome-private-chat|1|' + kind + '|' + identity).encode()
def seal(master, kind, identity, value):
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(direction(master, kind)).encrypt(nonce, json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode(), aad(kind, identity))
    return dict(v=1, id=identity, nonce=b64(nonce), ct=b64(ciphertext))
def open_envelope(master, kind, envelope):
    if envelope.get('v') != 1 or set(envelope) != {'v', 'id', 'nonce', 'ct'}:
        raise ValueError('Invalid envelope')
    nonce = unb64(envelope['nonce'])
    if len(nonce) != 12:
        raise ValueError('Invalid nonce')
    return json.loads(AESGCM(direction(master, kind)).decrypt(nonce, unb64(envelope['ct']), aad(kind, envelope['id'])))
