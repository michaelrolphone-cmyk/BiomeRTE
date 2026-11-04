import ast,contextlib,io,json,time,unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

class DiagnosticsTests(unittest.TestCase):
    def adapter(self,response):
        source=ast.parse(Path(__file__).with_name('private_chat.py').read_text())
        functions=[n for n in source.body if isinstance(n,ast.FunctionDef) and n.name in ('inference','_inference')]
        self.seen=[]
        class Opener:
            def open(_,request):self.seen.append(json.loads(request.data));return io.StringIO(json.dumps(response))
        class Runtime:
            @contextlib.contextmanager
            def slot(_,kind,model):self.assertEqual(('llm','selected-key'),(kind,model));yield 'loaded-instance'
        class ModelError(ValueError):pass
        def clean(messages):return [{'role':m['role'],'content':m['content']} for m in messages]
        env={'time':time,'json':json,'Request':Request,'ModelRuntime':Runtime,'ModelError':ModelError,'privacy_ready':lambda:None,'conversation_messages':clean,'LOCAL':Opener(),'LM':Path('/nonexistent-test-root')}
        exec(compile(ast.Module(body=functions,type_ignores=[]),'<real adapter functions>','exec'),env);return env
    def response(self):return {'model':'loaded-instance','usage':{'prompt_tokens':7,'completion_tokens':2,'total_tokens':9,'secret':'drop'},'choices':[{'finish_reason':'stop','message':{'content':'synthetic'}}]}
    def test_reports_authenticated_instance_mapping_timings_and_safe_usage(self):
        e=self.adapter(self.response());messages=[{'role':'system','content':'thread instructions'},{'role':'user','content':'synthetic'}];result=e['inference'](messages,'selected-key');d=result['diagnostics'];self.assertTrue(d['model_verified']);self.assertEqual('selected-key',d['requested_model']);self.assertEqual('loaded-instance',d['response_model']);self.assertEqual('loaded-instance',d['resolved_model']);self.assertEqual({'prompt_tokens':7,'completion_tokens':2,'total_tokens':9},d['usage']);self.assertGreaterEqual(d['admission_ms'],0);self.assertGreaterEqual(d['inference_ms'],0);self.assertEqual(messages,self.seen[0]['messages']);self.assertFalse(self.seen[0]['store'])
    def test_wrong_and_missing_reported_models_are_rejected(self):
        for actual in ['wrong',None]:
            r=self.response();r['model']=actual;e=self.adapter(r)
            with self.assertRaises(e['ModelError']):e['inference']([],'selected-key')
    def test_usage_diagnostics_do_not_include_invalid_counts(self):
        r=self.response();r['usage']={'prompt_tokens':True,'completion_tokens':-1,'total_tokens':'9'};e=self.adapter(r);self.assertEqual({},e['inference']([],'selected-key')['diagnostics']['usage'])
if __name__=='__main__':unittest.main()
