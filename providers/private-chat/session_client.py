"""Reference client for interoperability and synthetic deployment qualification."""
import hashlib
import secrets
import uuid
from cryptography.hazmat.primitives.asymmetric import ed25519,x25519
from session_protocol import b64,raw,public,client_transcript,transcript,keys,seal,open_message
class SessionClient:
    def __init__(self,server_identity,identity=None):
        self.server_identity=server_identity
        self.identity=identity or ed25519.Ed25519PrivateKey.generate()
        self.ephemeral=x25519.X25519PrivateKey.generate()
        self.sid=str(uuid.uuid4())
        self.hello=dict(v=2,kind='hello',id=str(uuid.uuid4()),sid=self.sid,
                        client_identity=b64(public(self.identity)),client_ephemeral=b64(public(self.ephemeral)),client_nonce=b64(secrets.token_bytes(32)))
        self.hello['signature']=b64(self.identity.sign(client_transcript(self.hello)))
    def accept(self,reply):
        if reply['v']!=2 or reply['kind']!='hello' or reply['id']!=self.hello['id'] or reply['sid']!=self.sid:
            raise ValueError('Mismatched server hello')
        whole=transcript(self.hello,reply,self.server_identity)
        ed25519.Ed25519PublicKey.from_public_bytes(self.server_identity).verify(raw(reply['signature'],64),whole)
        self.thash=hashlib.sha256(whole).digest()
        self.traffic=keys(self.ephemeral.exchange(x25519.X25519PublicKey.from_public_bytes(raw(reply['server_ephemeral'],32))),self.thash)
        self.ephemeral=None
    def request(self,messages,enrollment_token=None):
        data={'messages':messages}
        if enrollment_token is not None:
            data['enrollment_token']=b64(enrollment_token)
        self.operation=str(uuid.uuid4())
        return seal(self.traffic['request'],'request',self.sid,self.operation,self.thash,data)
    def response(self,envelope):
        value=open_message(self.traffic['response'],'response',self.sid,self.operation,self.thash,envelope)
        self.traffic=None
        return value
