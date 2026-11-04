import copy
import hashlib
import secrets
import unittest
from cryptography.exceptions import InvalidSignature,InvalidTag
from cryptography.hazmat.primitives.asymmetric import ed25519
from session_protocol import *
from session_client import SessionClient
class SessionTests(unittest.TestCase):
    def setUp(self):
        self.server_key=ed25519.Ed25519PrivateKey.generate()
        self.token=secrets.token_bytes(32)
        self.enrolled=[]
        self.server=SessionServer(self.server_key,hashlib.sha256(self.token).digest(),enrolled=self.enrolled.append)
        self.client=SessionClient(public(self.server_key))
        self.messages=[{'role':'user','content':'synthetic private canary 🧬'}]
    def handshake(self):
        h=self.client.hello
        self.client.accept(self.server.handle(h,h['id'],None))
    def test_roundtrip_one_use_and_enrollment(self):
        self.handshake()
        request=self.client.request(self.messages,self.token)
        reply=self.server.handle(request,request['id'],lambda messages:{'text':'test reply'})
        self.assertEqual(self.client.response(reply),{'text':'test reply'})
        self.assertFalse(self.server.sessions)
        self.assertEqual(self.enrolled,[public(self.client.identity)])
        self.assertIsNone(self.server.enrollment_hash)
        with self.assertRaises(ValueError):
            self.server.handle(request,request['id'],lambda _:self.fail('replayed'))
    def test_wrong_mac_pin_and_modified_hello(self):
        hello=self.client.hello
        reply=self.server.handle(hello,hello['id'],None)
        impostor=SessionClient(public(ed25519.Ed25519PrivateKey.generate()),self.client.identity)
        impostor.hello=hello;impostor.sid=hello['sid']
        with self.assertRaises(InvalidSignature):impostor.accept(reply)
        reply['server_nonce']=b64(secrets.token_bytes(32))
        with self.assertRaises(InvalidSignature):self.client.accept(reply)
    def test_phone_signature_and_enrollment_required(self):
        hello=copy.deepcopy(self.client.hello)
        hello['client_nonce']=b64(secrets.token_bytes(32))
        with self.assertRaises(InvalidSignature):self.server.handle(hello,hello['id'],None)
        self.handshake()
        request=self.client.request(self.messages,secrets.token_bytes(32))
        with self.assertRaises(ValueError):self.server.handle(request,request['id'],lambda _:self.fail('unauthorized inference'))
        self.assertFalse(self.enrolled)
    def test_tamper_and_binding(self):
        self.handshake();r=self.client.request(self.messages,self.token)
        bad=dict(r,ct=b64(base64.b64decode(r['ct'])[:-1]+bytes([base64.b64decode(r['ct'])[-1]^1])))
        with self.assertRaises(InvalidTag):self.server.handle(bad,bad['id'],None)
        bad=dict(r,id=str(uuid.uuid4()))
        with self.assertRaises(InvalidTag):self.server.handle(bad,bad['id'],None)
        with self.assertRaises(ValueError):self.server.handle(r,str(uuid.uuid4()),None)
    def test_restart_forgets_session_and_new_exchange_is_fresh(self):
        self.handshake();r=self.client.request(self.messages,self.token)
        restarted=SessionServer(self.server_key,hashlib.sha256(self.token).digest())
        with self.assertRaises(ValueError):restarted.handle(r,r['id'],None)
        another=SessionClient(public(self.server_key),self.client.identity)
        h=another.hello;another.accept(self.server.handle(h,h['id'],None))
        self.assertNotEqual(another.traffic['request'],self.client.traffic['request'])
    def test_paired_phone_no_enrollment_token_and_wrong_phone_rejected(self):
        self.handshake();r=self.client.request(self.messages,self.token)
        self.server.handle(r,r['id'],lambda _:{'text':'paired'})
        again=SessionClient(public(self.server_key),self.client.identity)
        h=again.hello;again.accept(self.server.handle(h,h['id'],None))
        r=again.request(self.messages)
        self.assertEqual(again.response(self.server.handle(r,r['id'],lambda _:{'text':'again'})),{'text':'again'})
        other=SessionClient(public(self.server_key))
        with self.assertRaises(ValueError):self.server.handle(other.hello,other.hello['id'],None)
    def test_saved_assistant_model_label_is_not_forwarded_to_inference(self):
        self.handshake()
        messages=self.messages+[{'role':'assistant','content':'prior reply','model':'previous-model'}, {'role':'user','content':'continue'}]
        request=self.client.request(messages,self.token)
        seen=[]
        reply=self.server.handle(request,request['id'],lambda history:seen.append(history) or {'text':'continued'})
        self.assertEqual(self.client.response(reply),{'text':'continued'})
        self.assertEqual(seen[0][1],{'role':'assistant','content':'prior reply'})
    def test_unrecognized_history_fields_still_rejected_before_inference(self):
        self.handshake()
        request=self.client.request([{'role':'assistant','content':'prior','tool_calls':[]}],self.token)
        with self.assertRaises(ValueError):self.server.handle(request,request['id'],lambda _:self.fail('invalid history reached inference'))
if __name__=='__main__':unittest.main()

