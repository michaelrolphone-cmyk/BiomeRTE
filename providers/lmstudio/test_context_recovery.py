import contextlib
import copy
import json
import tempfile
import unittest
from pathlib import Path
from context_recovery import complete


class FakeServer:
    def __init__(self, size=16, maximum=64, responses=None):
        self.size=size; self.maximum=maximum; self.instance='qwen-instance'
        self.responses=list(responses or [])
        self.requests=[]; self.loads=[]; self.unloads=[]; self.loaded=True
        self.actual_size=None
    def __call__(self, path, body=None):
        if path=='/api/v1/models':
            return {'models':[{'key':'qwen-key','max_context_length':self.maximum,
                'loaded_instances':[{'id':self.instance,'config':{'context_length':self.size,
                    'parallel':4,'flash_attention':True}}] if self.loaded else []}]}
        if path.endswith('/unload'):
            self.unloads.append(body);self.loaded=False;return {}
        if path.endswith('/load'):
            self.loads.append(body);self.loaded=True
            self.size=self.actual_size or body['context_length']
            self.instance='qwen-expanded'
            return {'instance_id':self.instance}
        self.requests.append(copy.deepcopy(body))
        response=self.responses.pop(0)
        if isinstance(response,Exception):raise response
        return dict(response,model=response.get('model',self.instance))


def response(finish='stop', prompt=10, output=5, **extra):
    return dict({'choices':[{'finish_reason':finish,'message':{'content':'{"ok":true}'}}],
                 'usage':{'prompt_tokens':prompt,'completion_tokens':output,'total_tokens':prompt+output}},**extra)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.body={'model':'qwen-instance','messages':[{'role':'user','content':'unchanged'}],
                   'response_format':{'type':'json_schema','json_schema':{'schema':{'type':'object'}}},
                   'max_tokens':-1,'stream':False}
    def test_context_boundary_grows_and_keeps_exact_input_schema_and_unlimited_output(self):
        server=FakeServer(responses=[response('length'),response()])
        original=copy.deepcopy(self.body)
        result=complete(server,self.body)
        self.assertEqual(self.body,original)
        self.assertEqual(len(server.requests),2)
        self.assertEqual(server.loads[0],{'model':'qwen-key','context_length':32,'parallel':4,'flash_attention':True})
        for req in server.requests:
            self.assertEqual(req['messages'],original['messages'])
            self.assertEqual(req['response_format'],original['response_format'])
            self.assertEqual(req['max_tokens'],-1)
        self.assertEqual(result['biome_inference']['attempts'][0]['finish_reason'],'length')
        self.assertEqual(result['biome_inference']['attempts'][1]['context_length'],32)
    def test_verified_capacity_is_reused_after_model_eviction(self):
        with tempfile.TemporaryDirectory() as d:
            cache=Path(d)/'context.json'
            first=FakeServer(responses=[response('length'),response()])
            complete(first,self.body,cache)
            second=FakeServer(responses=[response()])
            complete(second,self.body,cache)
            self.assertEqual(len(second.requests),1)
            self.assertEqual(second.loads[0]['context_length'],32)
            self.assertEqual(json.loads(cache.read_text()),{'qwen-key':32})
    def test_growth_stops_at_actual_model_capacity(self):
        server=FakeServer(size=16,maximum=24,responses=[response('length'),response('length',18,5)])
        with self.assertRaisesRegex(ValueError,'max_context_length.*24'):complete(server,self.body)
        self.assertEqual([x['context_length'] for x in server.loads],[24])
        self.assertEqual(len(server.requests),2)
    def test_no_retry_for_short_output_cap_or_missing_usage(self):
        for res,cap in [(response('length',2,3),-1),(response('length'),5),
                        ({'choices':[{'finish_reason':'length','message':{'content':''}}]},-1)]:
            with self.subTest(res=res,cap=cap):
                server=FakeServer(responses=[res])
                with self.assertRaisesRegex(ValueError,'finish reason: length'):
                    complete(server,dict(self.body,max_tokens=cap))
                self.assertEqual(server.loads,[])
                self.assertEqual(len(server.requests),1)
    def test_transport_error_is_not_replayed(self):
        server=FakeServer(responses=[ConnectionError('lost response')])
        with self.assertRaises(ConnectionError):complete(server,self.body)
        self.assertEqual(len(server.requests),1)
    def test_ignored_resize_is_not_retried(self):
        server=FakeServer(responses=[response('length')]);server.actual_size=16
        with self.assertRaisesRegex(RuntimeError,'not confirmed'):complete(server,self.body)
        self.assertEqual(len(server.requests),1)
    def test_wrong_model_is_rejected(self):
        server=FakeServer(responses=[response(model='wrong')])
        with self.assertRaisesRegex(RuntimeError,'different model'):complete(server,self.body)
    def test_tool_calls_and_streaming_are_not_replayed(self):
        for extra in ({'tools':[{'name':'edit'}]},{'tool_choice':'auto'},{'stream':True}):
            server=FakeServer(responses=[])
            with self.assertRaises(ValueError):complete(server,dict(self.body,**extra))
            self.assertEqual(server.requests,[])
        res=response('length');res['choices'][0]['message']['tool_calls']=[{'id':'edit1'}]
        server=FakeServer(responses=[res])
        with self.assertRaises(ValueError):complete(server,self.body)
        self.assertEqual(server.loads,[])
    def test_dynamic_policy_shrinks_cached_64k_and_never_reuses_cache_as_ram_authority(self):
        with tempfile.TemporaryDirectory() as d:
            cache=Path(d)/'context.json';cache.write_text('{"qwen-key":65536}')
            server=FakeServer(size=65536,maximum=262144,responses=[response()])
            seen=[]
            def policy(model,loaded,size,maximum):
                seen.append(size);return {'target':32768,'available_bytes':1024}
            result=complete(server,self.body,cache,context_selector=policy)
            self.assertEqual([x['context_length'] for x in server.loads],[32768])
            self.assertEqual(result['biome_inference']['attempts'][0]['context_length'],32768)
            self.assertEqual(seen,[65536,32768])
    def test_pressure_increase_during_growth_shrinks_before_predicting(self):
        server=FakeServer(size=32768,maximum=262144,responses=[response()])
        picks=iter([65536,32768])
        result=complete(server,self.body,context_selector=lambda *a:{'target':next(picks)})
        self.assertEqual([x['context_length'] for x in server.loads],[65536,32768])
        self.assertEqual(result['biome_inference']['attempts'][0]['context_length'],32768)
    def test_ignored_shrink_is_a_failure(self):
        server=FakeServer(size=65536,maximum=262144);server.actual_size=65536
        with self.assertRaisesRegex(RuntimeError,'not confirmed'):
            complete(server,self.body,context_selector=lambda *a:{'target':32768})
        self.assertEqual(server.requests,[])
    def test_context_failure_does_not_override_current_memory_allowance(self):
        server=FakeServer(size=32768,maximum=262144,responses=[response('length',20000,12767)])
        with self.assertRaisesRegex(ValueError,'memory allowance'):
            complete(server,self.body,context_selector=lambda *a:{'target':32768})
        self.assertEqual(server.loads,[])
    def test_logs_include_usage_but_not_prompt_or_response(self):
        logs=[];server=FakeServer(responses=[response()])
        complete(server,self.body,log=logs.append)
        self.assertIn('prompt_tokens',logs[0])
        self.assertNotIn('unchanged',str(logs));self.assertNotIn('ok',str(logs).replace('tokens',''))
    def test_maximum_change_does_not_apply_stale_cached_capacity(self):
        with tempfile.TemporaryDirectory() as d:
            cache=Path(d)/'context.json';cache.write_text('{"qwen-key":128}')
            server=FakeServer(responses=[response()])
            complete(server,self.body,cache)
            self.assertEqual(server.loads,[])

if __name__=='__main__':unittest.main()
