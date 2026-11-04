import ast
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch
from model_runtime import ModelRuntime, LocalModels

class FakeModels(LocalModels):
    def __init__(self):
        self.loaded={'qwen-instance':'qwen'};self.image_loaded=False;self.calls=[]
    def config(self):return {'default_llm_model':'qwen','default_load':{'context_length':16384}}
    def models(self):return [{'type':'llm','key':key,'loaded_instances':[{'id':i} for i,k in self.loaded.items() if k==key]} for key in ['qwen','llama']]
    def lm(self,path,body=None):
        self.calls.append((path,body))
        if path.endswith('/unload'):self.loaded.pop(body['instance_id']);return body
        if path.endswith('/load'):
            assert not self.image_loaded,'Cannot load LLM beside image models'
            ident=body['model']+'-instance';self.loaded[ident]=body['model'];return {'instance_id':ident}
        raise AssertionError(path)
    def comfy(self,path,body=None):
        self.calls.append((path,body))
        if body:self.image_loaded=False
        return {'running':0,'pending':0,'loaded_models':int(self.image_loaded)}

class RuntimeTests(unittest.TestCase):
    def test_switch_unloads_other_llms_before_loading_exact_requested_model(self):
        m=FakeModels();self.assertEqual('llama-instance',m.prepare('llm','llama'))
        self.assertEqual({'llama-instance':'llama'},m.loaded)
        paths=[x[0] for x in m.calls];self.assertLess(paths.index('/api/v1/models/unload'),paths.index('/api/v1/models/load'))
    def test_image_admission_unloads_all_llms_and_llm_releases_image_cache(self):
        m=FakeModels();m.prepare('image',None);self.assertEqual({},m.loaded);m.image_loaded=True
        self.assertEqual('qwen-instance',m.prepare('llm','qwen'));self.assertFalse(m.image_loaded)
    def test_unavailable_model_does_not_unload_selected_conversation(self):
        m=FakeModels()
        with self.assertRaises(ValueError):m.select('missing')
        self.assertEqual({'qwen-instance':'qwen'},m.loaded)
    def test_idle_default_respects_selected_chat_model_and_restores_qwen_when_empty(self):
        m=FakeModels();m.loaded={'custom-instance':'llama'}
        self.assertFalse(m.warm_default());self.assertEqual({'custom-instance':'llama'},m.loaded)
        m.loaded={};self.assertTrue(m.warm_default());self.assertEqual({'qwen-instance':'qwen'},m.loaded)
    def test_fifo_serializes_image_and_multiple_llm_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            m=FakeModels();r=ModelRuntime(tmp,m);entered=threading.Event();release=threading.Event();order=[];errors=[]
            def job(kind,model,block=False):
                try:
                    with r.slot(kind,model) as selected:
                        order.append((kind,selected))
                        if kind=='image':m.image_loaded=True;self.assertFalse(m.loaded)
                        else:self.assertFalse(m.image_loaded)
                        if block:entered.set();release.wait(5)
                except Exception as e:errors.append(e)
            a=threading.Thread(target=job,args=('image',None,True));a.start();self.assertTrue(entered.wait(3))
            b=threading.Thread(target=job,args=('llm','llama'));b.start()
            for _ in range(100):
                if len(r.state())==2:break
                time.sleep(.01)
            c=threading.Thread(target=job,args=('llm','qwen'));c.start();time.sleep(.1)
            self.assertEqual([('image',None)],order);self.assertFalse(r.warm_default());release.set()
            for t in (a,b,c):t.join(5);self.assertFalse(t.is_alive())
            self.assertEqual([],errors);self.assertEqual([('image',None),('llm','llama-instance'),('llm','qwen-instance')],order);self.assertEqual([],r.state())
    def test_inference_failure_releases_admission_for_next_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=ModelRuntime(tmp,FakeModels())
            with self.assertRaises(RuntimeError):
                with r.slot('image'):raise RuntimeError('synthetic failure')
            with r.slot('llm','qwen') as selected:self.assertEqual('qwen-instance',selected)
            self.assertEqual([],r.state())
    def test_crashed_owner_releases_lock_and_is_cleaned_without_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            script="from model_runtime import ModelRuntime;import sys,time\nclass M:\n def prepare(self,*a):return None\n def release(self,*a):pass\nr=ModelRuntime(sys.argv[1],M())\nwith r.slot('image'):\n print('OWNED',flush=True)\n time.sleep(600)\n"
            env=dict(os.environ,PYTHONPATH=str(Path(__file__).parent.resolve()))
            child=subprocess.Popen([sys.executable,'-c',script,tmp],stdout=subprocess.PIPE,text=True,env=env)
            try:
                self.assertEqual('OWNED',child.stdout.readline().strip());child.kill();child.wait(5)
                r=ModelRuntime(tmp,FakeModels())
                with r.slot('llm','qwen'):self.assertEqual(1,len(r.state()))
                self.assertEqual([],r.state())
            finally:
                if child.poll() is None:child.kill();child.wait(5)
                child.stdout.close()
    def test_metadata_queue_never_contains_request_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=ModelRuntime(tmp,FakeModels())
            with r.slot('llm','qwen'):
                state=r.state()[0];self.assertEqual({'ticket','id','pid','kind','model','phase'},set(state));self.assertEqual('running',state['phase'])
    def test_missing_model_is_rejected_before_queueing(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=ModelRuntime(tmp,FakeModels())
            with self.assertRaises(ValueError):
                with r.slot('llm'):pass
            self.assertEqual([],r.state())

class AdapterTests(unittest.TestCase):
    def agent(self):
        stub=types.ModuleType('mac_provider')
        with patch.dict(sys.modules,{'mac_provider':stub}):
            spec=importlib.util.spec_from_file_location('tested_agent',Path(__file__).with_name('lmstudio_agent.py'));m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        return m
    def test_coding_default_and_explicit_task_model(self):
        a=self.agent();self.assertEqual('qwen3.5-9b',a.task_model({'context':{'coding':True}}));self.assertEqual('llama',a.task_model({'context':{'model':'llama'}}))
        with self.assertRaises(ValueError):a.task_model({'context':{'model':None}})
    def test_agent_discovers_without_loading_outside_admission(self):
        a=self.agent();calls=[];a.api=lambda path: calls.append(path) or {'models':[{'key':'qwen3.5-9b'}]}
        self.assertEqual('qwen3.5-9b',a.model_ready());self.assertEqual(['/api/v1/models'],calls)
    def test_each_agent_inference_runs_inside_model_admission_with_exact_instance(self):
        a=self.agent();active=[];calls=[]
        class Runtime:
            @contextlib.contextmanager
            def slot(self,kind,model):active.append((kind,model));yield 'resolved-instance';active.pop()
        a.ModelRuntime=Runtime
        def api(path,body):
            self.assertEqual([('llm','llama')],active);calls.append(body);return {'model':'resolved-instance'}
        a._api=api;self.assertEqual({'model':'resolved-instance'},a.api('/v1/chat/completions',{'model':'llama','messages':[]}));self.assertEqual('resolved-instance',calls[0]['model']);self.assertEqual([],active)
        with self.assertRaises(ValueError):a.api('/v1/chat/completions',{'messages':[]})
    def test_agent_rejects_response_from_wrong_model(self):
        a=self.agent()
        class Runtime:
            @contextlib.contextmanager
            def slot(self,*args):yield 'expected'
        a.ModelRuntime=Runtime;a._api=lambda *args:{'model':'wrong'}
        with self.assertRaises(RuntimeError):a.api('/v1/chat/completions',{'model':'qwen'})
    def test_private_chat_and_image_execution_hold_shared_admission(self):
        active=[]
        class Runtime:
            @contextlib.contextmanager
            def slot(self,kind,model=None,progress=None):
                active.append(kind);yield 'exact-instance';active.pop()
        tree=ast.parse(Path(__file__).with_name('private_chat.py').read_text())
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='inference')
        def inference(messages,model):
            self.assertEqual(['llm'],active);self.assertEqual('exact-instance',model);return {'text':'answer','diagnostics':{}}
        env={'time':time,'ModelRuntime':Runtime,'ModelError':ValueError,'_inference':inference}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'<isolated inference>','exec'),env)
        self.assertEqual('answer',env['inference']([], 'selected')['text']);self.assertEqual([],active)
        with self.assertRaises(ValueError):env['inference']([],None)
        import comfy_backend
        with patch.object(comfy_backend,'ModelRuntime',Runtime):
            backend=comfy_backend.ComfyBackend()
            def generate(messages,progress):self.assertEqual(['image'],active);return {'images':[]}
            backend._generate=generate
            self.assertEqual({'images':[]},backend.generate([{'role':'user','content':'synthetic'}]))
            self.assertEqual([],active)
    def test_image_instructions_preserve_explicit_parameters(self):
        from comfy_backend import ComfyBackend
        spec=ComfyBackend().specification([{'role':'system','content':'Thread instructions'},{'role':'user','content':'{"prompt":"synthetic","steps":3,"seed":4}'}])
        self.assertEqual('Thread instructions\n\nsynthetic',spec['prompt']);self.assertEqual(3,spec['steps']);self.assertEqual(4,spec['seed'])

if __name__=='__main__':unittest.main()
