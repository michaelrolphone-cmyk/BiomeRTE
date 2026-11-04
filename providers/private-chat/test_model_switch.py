import io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import model_service as ms
from test_model_selection import ModelTests

class Backend:
    def __init__(self):
        self.loaded={'a':'old-instance-a'};self.calls=[];self.busy=0;self.fail_load=False
    def open(self,req):
        return io.StringIO(json.dumps({'models':[{'type':'llm','key':k,
            'loaded_instances':[{'id':self.loaded[k]}] if k in self.loaded else []} for k in ['a','b']]}))
    def cli(self,*args):
        self.calls.append(args)
        if args[0]=='ps':
            return json.dumps([{'type':'llm','modelKey':k,'identifier':v,
                'status':'predicting' if self.busy else 'idle','queued':0} for k,v in self.loaded.items()])
        if args[0]=='unload':
            assert not self.busy,'Must not unload an active model'
            self.loaded={k:v for k,v in self.loaded.items() if v!=args[1]};return ''
        if args[0]=='load':
            assert not self.loaded,'Previous model must be released before target load'
            if self.fail_load:raise ms.ModelSwitchError('model_load_failed')
            self.loaded[args[1]]=args[3];return ''
        raise AssertionError(args)

class SwitchTests(unittest.TestCase):
    def test_unloads_pinned_idle_model_before_acknowledging_target(self):
        b=Backend()
        with tempfile.TemporaryDirectory() as p,patch.object(ms,'_cli',b.cli):
            result=ms.switch_model('b',b,Path(p))
        self.assertEqual(b.loaded,{'b':'b'});self.assertEqual(result['model'],'b')
        self.assertEqual([m['loaded'] for m in result['models']],[False,True])
        self.assertLess(b.calls.index(('unload','old-instance-a')),next(i for i,a in enumerate(b.calls) if a[0]=='load'))
    def test_waits_for_active_work_without_cancelling(self):
        b=Backend();b.busy=1
        def completed(_):b.busy=0
        with tempfile.TemporaryDirectory() as p,patch.object(ms,'_cli',b.cli),patch.object(ms.time,'sleep',side_effect=completed) as wait:
            ms.switch_model('b',b,Path(p))
        wait.assert_called_once();self.assertEqual(b.loaded,{'b':'b'})
    def test_invalid_selection_does_not_unload_anything(self):
        b=Backend()
        with tempfile.TemporaryDirectory() as p,patch.object(ms,'_cli',b.cli):
            with self.assertRaises(ValueError):ms.switch_model('missing',b,Path(p))
        self.assertEqual(b.calls,[]);self.assertEqual(b.loaded,{'a':'old-instance-a'})
    def test_old_instance_alias_resolves_to_stable_key(self):
        b=Backend()
        with tempfile.TemporaryDirectory() as p,patch.object(ms,'_cli',b.cli):
            r=ms.switch_model('old-instance-a',b,Path(p))
        self.assertEqual(r['model'],'a');self.assertFalse(any(c[0]=='load' for c in b.calls))
    def test_old_clients_automatically_switch_before_inference(self):
        b=Backend()
        with tempfile.TemporaryDirectory() as p,patch.object(ms,'_cli',b.cli):
            self.assertEqual(ms.ensure_selected_model('b',b,Path(p)),'b')
        self.assertEqual(b.loaded,{'b':'b'})
    def test_failed_load_cannot_report_success(self):
        b=Backend();b.fail_load=True
        with tempfile.TemporaryDirectory() as p,patch.object(ms,'_cli',b.cli):
            with self.assertRaises(ms.ModelSwitchError):ms.switch_model('b',b,Path(p))
        self.assertNotIn('b',b.loaded)
    def test_authenticated_switch_control_carries_no_conversation(self):
        helper=ModelTests();received=[]
        server,client=helper.session(lambda body:received.append(body) or {'model':body['model'],'models':[]})
        req=helper.request(client,{'action':'select_model','model':'b'})
        result=client.response(server.handle(req,req['id'],None))
        self.assertEqual(result['model'],'b');self.assertEqual(received,[{'action':'select_model','model':'b'}])
        with self.assertRaises(ValueError):server.handle(req,req['id'],None)
    def test_bad_switch_control_does_not_consume_session(self):
        helper=ModelTests();server,client=helper.session(lambda body:self.fail())
        req=helper.request(client,{'action':'select_model','model':{}})
        with self.assertRaises(ValueError):server.handle(req,req['id'],None)
        self.assertIn(client.sid,server.sessions)
if __name__=='__main__':unittest.main()
