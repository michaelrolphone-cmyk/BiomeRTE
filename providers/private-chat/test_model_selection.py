import io,json,tempfile,unittest,uuid
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric import ed25519
from session_protocol import SessionServer,public,seal
from session_client import SessionClient
from model_service import model_catalog,selected_model

class Opener:
    def __init__(self): self.requests=[]
    def open(self,req):
        self.requests.append(req)
        return io.StringIO(json.dumps({'models':[
            {'type':'llm','key':'model-a','display_name':'Model A','loaded_instances':[{'id':'loaded-a'}]},
            {'type':'embedding','key':'embedding'},
            {'type':'llm','key':'model-b','display_name':'Model B','loaded_instances':[]}]}))

class ModelTests(unittest.TestCase):
    def session(self,handler):
        key,phone=ed25519.Ed25519PrivateKey.generate(),ed25519.Ed25519PrivateKey.generate()
        server=SessionServer(key,phone_identity=public(phone),request_handler=handler)
        client=SessionClient(public(key),phone)
        client.accept(server.handle(client.hello,client.hello['id'],None))
        return server,client
    def request(self,client,body):
        client.operation=str(uuid.uuid4())
        return seal(client.traffic['request'],'request',client.sid,client.operation,client.thash,body)
    def test_encrypted_catalog_no_chat_history_or_inference(self):
        called=[]
        server,client=self.session(lambda body: called.append(body) or {'models':[{'id':'model-b','name':'Model B'}]})
        req=self.request(client,{'action':'models'})
        self.assertNotIn('models',json.dumps(req))
        response=server.handle(req,req['id'],lambda _:self.fail('inference'))
        self.assertEqual(client.response(response)['models'][0]['id'],'model-b')
        self.assertEqual(called,[{'action':'models'}])
        with self.assertRaises(ValueError): server.handle(req,req['id'],None)
    def test_selected_model_inside_authenticated_envelope(self):
        server,client=self.session(lambda body:{'model':body['model'],'text':'synthetic'})
        req=self.request(client,{'messages':[{'role':'user','content':'test'}],'model':'model-b'})
        self.assertNotIn('model-b',json.dumps(req))
        response=server.handle(req,req['id'],None)
        self.assertEqual(client.response(response)['model'],'model-b')
    def test_catalog_filters_embeddings_and_preserves_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);(p/'api-token').write_text('test-only')
            opener=Opener();models=model_catalog(opener,p)
            self.assertEqual([m['id'] for m in models],['loaded-a','model-b'])
            self.assertTrue(models[0]['loaded']);self.assertFalse(models[1]['loaded'])
            self.assertEqual(opener.requests[0].get_header('Authorization'),'Bearer test-only')
    def test_selection_exact_and_legacy_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);o=Opener()
            self.assertEqual(selected_model(None,o,p),'qwen3.5-9b')
            self.assertEqual(selected_model('model-b',o,p),'model-b')
            for bad in ['',{},'embedding','missing']:
                with self.assertRaises(ValueError): selected_model(bad,o,p)
    def test_catalog_not_available_to_unpaired_phone(self):
        key,phone=ed25519.Ed25519PrivateKey.generate(),ed25519.Ed25519PrivateKey.generate()
        server=SessionServer(key,phone_identity=public(phone),request_handler=lambda _:self.fail())
        client=SessionClient(public(key))
        with self.assertRaises(ValueError): server.handle(client.hello,client.hello['id'],None)
if __name__=='__main__':unittest.main()
