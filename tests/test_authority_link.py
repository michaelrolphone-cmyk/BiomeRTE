import io
import json
from importlib.machinery import SourceFileLoader
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

link = SourceFileLoader('authority_link', str(pathlib.Path(__file__).resolve().parents[1] / 'deploy' / 'authority_link.py')).load_module()
NODE='a'*64
SID='b'*32
RID='c'*32


class AuthorityLinkTests(unittest.TestCase):
    def config(self):
        return dict(node=NODE,port=8740,admin_token='private-fixture',secret='s'*32)

    def request(self, **changes):
        value=dict(type='inspect',id=RID,authority=NODE,session=SID)
        value.update(changes)
        return value

    def test_only_exact_read_only_operation_reaches_loopback(self):
        observed=[]
        class Response(io.BytesIO):
            def __enter__(self):return self
            def __exit__(self,*args):self.close()
        def call(request,timeout):
            observed.append((request.full_url,json.loads(request.data.decode()),timeout))
            return Response(json.dumps(dict(authority=NODE,id=SID,generation=7)).encode())
        with patch.object(link.urllib.request,'urlopen',side_effect=call):
            value=link.inspect(self.config(),self.request())
        self.assertEqual(value['status'],200)
        url,body,timeout=observed[0]
        self.assertEqual(url,'http://127.0.0.1:8740/v1/call')
        self.assertEqual(body['operation'],'stream.inspect')
        self.assertEqual(body['arguments'],{'session':SID})
        self.assertEqual(body['id'],'authority-'+RID)
        self.assertEqual(body['realm'],'site')

    def test_foreign_authority_and_command_envelopes_never_reach_http(self):
        with patch.object(link.urllib.request,'urlopen') as call:
            for changes in ({'authority':'d'*64},{'type':'provider.result'},
                            {'id':'bad'},{'session':'bad'},{'inputs':{'argv':['bad']}}):
                with self.assertRaises(ValueError):
                    link.inspect(self.config(),self.request(**changes))
            call.assert_not_called()

    def test_revocation_is_distinct_from_unavailable(self):
        for status,expected in ((403,403),(404,404),(409,409),(429,429),(500,503),(503,503)):
            with patch.object(link.urllib.request,'urlopen',
                              side_effect=HTTPError('fixture',status,'fixture',{},None)):
                self.assertEqual(link.inspect(self.config(),self.request())['status'],expected)
        with patch.object(link.urllib.request,'urlopen',side_effect=TimeoutError()):
            self.assertEqual(link.inspect(self.config(),self.request())['status'],503)

    def test_wrong_binding_or_oversized_reply_never_becomes_grant(self):
        for value in (dict(authority='d'*64,id=SID),dict(authority=NODE,id='d'*32),'x'*16385):
            raw=json.dumps(value).encode()
            class Response(io.BytesIO):
                def __enter__(self):return self
                def __exit__(self,*args):self.close()
            with patch.object(link.urllib.request,'urlopen',return_value=Response(raw)):
                self.assertEqual(link.inspect(self.config(),self.request())['status'],503)

    def test_only_tls_outbound_url_and_private_existing_config_are_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            value=dict(node=NODE,stream_router_secret='s'*32,admin_token='fixture',
                       stream_gateway_url='wss://stream.example/')
            path=os.path.join(directory,'node.json')
            with open(path,'w') as target:json.dump(value,target)
            cfg=link.configuration(directory)
            self.assertEqual(cfg['url'],'wss://stream.example/authority/'+NODE+'/connect')
            for url in ('ws://stream.example','wss://user:pass@stream.example',
                        'wss://stream.example/path','wss://stream.example/?query=1'):
                value['stream_gateway_url']=url
                with open(path,'w') as target:json.dump(value,target)
                with self.assertRaises(ValueError):link.configuration(directory)


if __name__=='__main__':
    unittest.main()
