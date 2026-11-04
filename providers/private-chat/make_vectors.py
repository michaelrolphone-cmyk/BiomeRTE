"""Synthetic fixed inputs only; never production keys."""
import hashlib,json
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric import ed25519,x25519
from session_protocol import *
phone=ed25519.Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
server=ed25519.Ed25519PrivateKey.from_private_bytes(bytes(range(32,64)))
cp=x25519.X25519PrivateKey.from_private_bytes(bytes(range(64,96)))
sp=x25519.X25519PrivateKey.from_private_bytes(bytes(range(96,128)))
h=dict(v=2,kind='hello',id='00000000-0000-4000-8000-000000000001',sid='00000000-0000-4000-8000-000000000002',
       client_identity=b64(public(phone)),client_ephemeral=b64(public(cp)),client_nonce=b64(bytes(range(128,160))))
r=dict(server_ephemeral=b64(public(sp)),server_nonce=b64(bytes(range(160,192))))
whole=transcript(h,r,public(server));thash=hashlib.sha256(whole).digest()
traffic=keys(cp.exchange(sp.public_key()),thash)
a=aad('request',h['sid'],'00000000-0000-4000-8000-000000000003',thash)
vectors=dict(phone_public=public(phone).hex(),client_public=public(cp).hex(),server_public=public(sp).hex(),
             client_signature=phone.sign(client_transcript(h)).hex(),server_signature=server.sign(whole).hex(),
             transcript_hash=thash.hex(),request_key=traffic['request'].hex(),response_key=traffic['response'].hex(),
             cipher=AESGCM(traffic['request']).encrypt(bytes(range(12)),b'synthetic interoperability test',a).hex())
print(json.dumps(vectors))
