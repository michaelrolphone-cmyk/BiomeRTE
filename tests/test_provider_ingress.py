import base64
import types
import io
import json
import os
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

DEPLOY=os.path.join(os.path.dirname(__file__),'..','deploy')
sys.path.insert(0,DEPLOY)
import provider_ingress as ingress
ACTOR='a'*64
NODE='b'*64

def packet(controller=None):
    value={'site':'site-one','realm':'site','id':'same-id','operation':'provider.poll',
           'arguments':{'provider':ACTOR,'limit':1},
           'auth':{'actor':ACTOR,'signature':base64.b64encode(b'signature').decode()}}
    if controller is not None:value['controller']=controller
    return value

class Response(io.BytesIO):
    def getcode(self):return 200

class Opener:
    def __init__(self):self.requests=[]
    def open(self,request,timeout):
        self.requests.append((request,timeout))
        return Response(b'{"result":{"ok":true}}')

class ProviderIngressTests(unittest.TestCase):
    def result(self,value):return json.loads(value['content'][0]['text'])
    def test_legacy_raw_json_is_forwarded_without_operator_authority(self):
        opener=Opener();raw=json.dumps(packet()).replace('"limit": 1','"limit": 1.0, "big": 9007199254740993')
        self.assertEqual(self.result(ingress.dispatch_signed_provider({'envelope':raw},'http://127.0.0.1:4300','site-one',opener))['status'],200)
        request,timeout=opener.requests[0]
        self.assertEqual(request.data,raw.encode());self.assertEqual(request.full_url,'http://127.0.0.1:4300/v1/call')
        self.assertIsNone(request.get_header('Authorization'));self.assertEqual(timeout,30)
    def test_selected_signed_destination_is_preserved(self):
        opener=Opener();raw=json.dumps(packet(NODE))
        self.assertEqual(self.result(ingress.dispatch_signed_provider({'envelope':raw},'http://localhost:4300','site-one',opener,NODE))['status'],200)
        self.assertEqual(opener.requests[0][0].data,raw.encode())
    def test_wrong_or_missing_receiver_identity_never_forwards(self):
        for node in (None,'c'*64):
            opener=Opener()
            value=ingress.dispatch_signed_provider({'envelope':json.dumps(packet(NODE))},'http://localhost:4300','site-one',opener,node)
            self.assertEqual(self.result(value)['status'],400);self.assertEqual(opener.requests,[])
    def test_legacy_selector_is_not_injected_into_signed_body(self):
        opener=Opener();raw=json.dumps(packet())
        ingress.dispatch_signed_provider({'envelope':raw},'http://localhost:4300','site-one',opener,NODE)
        self.assertEqual(opener.requests[0][0].data,raw.encode())
    def test_nonloopback_and_wrong_site_do_not_forward(self):
        for local,site in [('https://evil.test','site-one'),('http://127.0.0.1:4300','another')]:
            opener=Opener();result=ingress.dispatch_signed_provider({'envelope':json.dumps(packet())},local,site,opener,NODE)
            self.assertIn(self.result(result)['status'],(400,500));self.assertEqual(opener.requests,[])
    def test_each_retry_reaches_live_native_authorization(self):
        class Revoked(Opener):
            def open(self,request,timeout):
                if self.requests:
                    self.requests.append((request,timeout))
                    raise HTTPError(request.full_url,403,'Revoked',{},io.BytesIO(b'{"error":"Revoked"}'))
                return super().open(request,timeout)
        opener=Revoked();args={'envelope':json.dumps(packet())}
        self.assertEqual(self.result(ingress.dispatch_signed_provider(args,'http://localhost:4300','site-one',opener))['status'],200)
        self.assertEqual(self.result(ingress.dispatch_signed_provider(args,'http://localhost:4300','site-one',opener))['status'],403)
        self.assertEqual(len(opener.requests),2)
    def test_pairing_forwards_only_candidate_and_proof(self):
        begin={'public_key':'-----BEGIN PUBLIC KEY-----\nexample\n-----END PUBLIC KEY-----','label':'relay'}
        finish={'challenge':'1'*64,'site':'site-one','key':ACTOR,'signature':base64.b64encode(b'proof').decode()}
        for phase,body in [('begin',begin),('finish',finish)]:
            opener=Opener();raw=json.dumps(body)
            value=ingress.dispatch_provider_pair({'phase':phase,'envelope':raw},'http://localhost:4300','site-one',opener)
            self.assertEqual(self.result(value)['status'],200)
            self.assertEqual(opener.requests[0][0].full_url,'http://localhost:4300/v1/pair/'+phase)
            self.assertEqual(opener.requests[0][0].data,raw.encode());self.assertIsNone(opener.requests[0][0].get_header('Authorization'))
        opener=Opener()
        self.assertEqual(self.result(ingress.dispatch_provider_pair({'phase':'approve','envelope':json.dumps(begin)},'http://localhost:4300','site-one',opener))['status'],400)
        self.assertEqual(opener.requests,[])
    def test_transport_metadata_cannot_enter_helper_unchecked(self):
        opener=Opener()
        result=ingress.dispatch_signed_provider({'envelope':json.dumps(packet()),'controller':NODE},'http://localhost:4300','site-one',opener,NODE)
        self.assertEqual(self.result(result)['status'],400);self.assertEqual(opener.requests,[])

class ProviderDispatchTests(unittest.TestCase):
    def module(self):
        path=os.path.join(DEPLOY,'gateway-agent.py');module=types.ModuleType('provider_destination_gateway');module.__file__=path
        with open(path,'rb') as handle:exec(compile(handle.read(),path,'exec'),module.__dict__)
        return module
    def test_both_handlers_validate_destination_before_stripping_metadata(self):
        module=self.module()
        for name in ('signed_provider_call','signed_provider_pair'):
            args={'envelope':json.dumps(packet()),'controller':NODE,'controller_owner':'pi'}
            if name.endswith('pair'):args['phase']='begin'
            job={'id':'j','request':{'name':name,'arguments':args}}
            target='dispatch_signed_provider' if name.endswith('call') else 'dispatch_provider_pair'
            with patch.object(ingress,target,return_value={'ok':True}) as run:
                self.assertEqual(module.dispatch(job,'http://localhost:4300',{'site':'site-one','node':NODE}),{'ok':True})
                self.assertNotIn('controller',run.call_args[0][0]);self.assertNotIn('controller_owner',run.call_args[0][0])
                with self.assertRaises(ValueError):module.dispatch(job,'http://localhost:4300',{'site':'site-one','node':'c'*64})
                self.assertEqual(run.call_count,1)
            self.assertEqual(args['controller'],NODE)
    def test_wire_destination_and_queue_destination_both_bind_receiver(self):
        module=self.module();opener=Opener()
        job={'id':'j','request':{'name':'signed_provider_call','arguments':{'envelope':json.dumps(packet('c'*64)),'controller':NODE,'controller_owner':'pi'}}}
        with patch.object(module,'opener',opener):value=module.dispatch(job,'http://localhost:4300',{'site':'site-one','node':NODE})
        self.assertEqual(json.loads(value['content'][0]['text'])['status'],400);self.assertEqual(opener.requests,[])
