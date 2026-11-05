import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

transport=types.ModuleType('mac_provider')
transport.save_json=lambda p,d: Path(p).write_text(json.dumps(d))
sys.modules['mac_provider']=transport

def module(name):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).with_name(name+'.py'))
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

agent=module('lmstudio_agent')
bridge=module('coding_mcp')

class CodingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.workspace=self.root/'workspace';self.workspace.mkdir()
        self.cfg={'workspace':str(self.workspace),'integrations':['mcp/files'],'context_length':16384,'terminal_resource':'terminal.test'}
        (self.root/'coding-config.json').write_text(json.dumps(self.cfg))
        self.wait={'id':'delegation-1','question':'Fix and test','context':{},'answer_schema':{'result':'obj'}}
        self.calls=[{'type':'tool_call','tool':'command_status','output':'{"exit_code":0}'}]
        self.response={'model_instance_id':'qwen3.5-9b','response_id':'resp_test','output':self.calls+[{'type':'message','content':'Fixed; tests passed.'}]}

    def test_workflow_schema_is_used_directly_without_text_double_encoding(self):
        schema={'type':'object','properties':{'facts':{'type':'array'}},'required':['facts']}
        self.wait['context'].update(coding=False,purpose='workflow_reasoning',response_schema=schema)
        response={'model':agent.MODEL,'choices':[{'finish_reason':'stop','message':{'content':'{"facts":[]}'}}]}
        with patch.object(agent,'model_ready'),patch.object(agent,'api',return_value=response) as api:
            result=agent.infer_text(self.wait)
        body=api.call_args.args[1]
        self.assertEqual(body['response_format']['json_schema']['schema'],schema)
        self.assertEqual(body['max_tokens'],-1)
        self.assertEqual(json.loads(result['result']['text']),{'facts':[]})
        self.assertEqual(result['result']['model'],agent.MODEL)

    def test_truncated_workflow_output_remains_a_failure(self):
        self.wait['context'].update(coding=False,purpose='workflow_reasoning',response_schema={})
        response={'model':agent.MODEL,'choices':[{'finish_reason':'length','message':{'content':'{"facts":[]}'}}]}
        with patch.object(agent,'model_ready'),patch.object(agent,'api',return_value=response):
            with self.assertRaisesRegex(ValueError,'finish reason: length'):agent.infer_text(self.wait)

    def test_generic_text_schema_preserves_existing_envelope(self):
        response={'model':agent.MODEL,'choices':[{'finish_reason':'stop','message':{'content':'{"text":"Hello"}'}}]}
        with patch.object(agent,'model_ready'),patch.object(agent,'api',return_value=response) as api:
            result=agent.infer_text(self.wait)
        self.assertEqual(result['result']['text'],'Hello')
        self.assertEqual(api.call_args.args[1]['response_format']['json_schema']['schema']['properties'],{'text':{'type':'string'}})

    def test_native_results_are_checkpointed_with_evidence(self):
        with patch.object(agent,'ROOT',self.root),patch.object(agent,'model_ready'),patch.object(agent,'api',return_value=self.response) as api:
            result=agent.infer(self.wait)
            self.assertEqual(result['result']['tool_calls'],self.calls)
            self.assertEqual(agent.infer(self.wait),result)
            api.assert_called_once()

    def test_interrupted_inference_does_not_replay_tools(self):
        with patch.object(agent,'ROOT',self.root),patch.object(agent,'model_ready'),patch.object(agent,'api',side_effect=ConnectionError) as api:
            with self.assertRaises(ConnectionError):agent.infer(self.wait)
            with self.assertRaisesRegex(RuntimeError,'interrupted'):agent.infer(self.wait)
            api.assert_called_once()

    def test_workspace_escape_rejected_before_inference(self):
        self.wait['context']['workspace']='..'
        with patch.object(agent,'ROOT',self.root),patch.object(agent,'api') as api:
            with self.assertRaises(ValueError):agent.infer(self.wait)
            api.assert_not_called()

    def test_text_only_route_preserved(self):
        self.wait['context']['coding']=False
        with patch.object(agent,'ROOT',self.root),patch.object(agent,'infer_text',return_value={'answer':'ok'}) as plain:
            self.assertEqual(agent.infer(self.wait),{'answer':'ok'})
            plain.assert_called_once_with(self.wait)

    def test_unavailable_task_model_rejected(self):
        with patch.object(agent,'api',return_value={'models':[]}):
            with self.assertRaisesRegex(RuntimeError,'unavailable'):agent.model_ready('missing')

    def test_model_loading_is_deferred_to_shared_admission(self):
        models={'models':[{'key':'chosen','loaded_instances':[]}]}
        with patch.object(agent,'ModelRuntime',Mock()),patch.object(agent,'api',return_value=models) as api:
            self.assertEqual(agent.model_ready('chosen'),'chosen')
            api.assert_called_once_with('/api/v1/models')

    def test_standalone_installation_still_loads_requested_model(self):
        models={'models':[{'key':'chosen','loaded_instances':[]}]}
        with patch.object(agent,'ROOT',self.root),patch.object(agent,'ModelRuntime',None),patch.object(agent,'api',side_effect=[models,{'status':'loaded','instance_id':'chosen'}]) as api:
            agent.model_ready('chosen')
            self.assertEqual(api.call_args.args,('/api/v1/models/load',{'model':'chosen','context_length':16384}))

    def test_context_recovery_remains_inside_shared_admission(self):
        from contextlib import contextmanager
        active=[]
        @contextmanager
        def slot(kind,model):
            active.append((kind,model))
            yield 'actual-instance'
            active.clear()
        runtime=Mock();runtime.slot=slot
        def recover(raw,body,cache,log,**kwargs):
            self.assertEqual(active,[('llm','chosen')])
            self.assertEqual(body['model'],'actual-instance')
            return {'model':'expanded-instance','choices':[]}
        with patch.object(agent,'ModelRuntime',return_value=runtime),patch.object(agent,'complete_text',side_effect=recover) as complete:
            self.assertEqual(agent.api('/v1/chat/completions',{'model':'chosen'})['model'],'expanded-instance')
        self.assertEqual(active,[])
        complete.assert_called_once()

    def test_coding_tools_do_not_enter_text_recovery(self):
        from contextlib import nullcontext
        runtime=Mock();runtime.slot.return_value=nullcontext('chosen')
        with patch.object(agent,'ModelRuntime',return_value=runtime),patch.object(agent,'complete_text') as recover,patch.object(agent,'_api',return_value={'model':'chosen'}) as raw:
            agent.api('/v1/chat/completions',{'model':'chosen','tools':[{'type':'function'}]})
        recover.assert_not_called()
        raw.assert_called_once()

    def test_mcp_token_cannot_be_forwarded_to_another_endpoint(self):
        self.cfg.update(mcp_port=48731,integrations=[{'type':'ephemeral_mcp','server_url':'https://example.com/mcp'}])
        (self.root/'coding-config.json').write_text(json.dumps(self.cfg))
        (self.root/'mcp-token').write_text('private-test-token')
        with patch.object(agent,'ROOT',self.root),patch.object(agent,'model_ready'),patch.object(agent,'api') as api:
            with self.assertRaisesRegex(ValueError,'localhost'):agent.infer(self.wait)
            api.assert_not_called()

    def test_tool_call_round_trip_records_real_mcp_result(self):
        self.cfg.update(inference_api='openai-tools',integrations=[{'allowed_tools':['read_text_file']}])
        (self.root/'coding-config.json').write_text(json.dumps(self.cfg))
        tool_result={'content':[{'type':'text','text':'actual file contents'}]}
        def mcp_call(method,params=None):
            if method=='initialize':return {}
            if method=='tools/list':return {'tools':[{'name':'read_text_file','inputSchema':{'type':'object'}}]}
            self.assertEqual(params,{'name':'read_text_file','arguments':{'path':'fixture.txt'}})
            return tool_result
        responses=[{'choices':[{'finish_reason':'tool_calls','message':{'role':'assistant','content':None,'tool_calls':[{'id':'call_1','type':'function','function':{'name':'read_text_file','arguments':'{"path":"fixture.txt"}'}}]}}]},
                   {'model':agent.MODEL,'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':'Read the file.'}}]}]
        with patch.object(agent,'ROOT',self.root),patch.object(agent,'model_ready'),patch.object(agent,'coding_mcp',side_effect=mcp_call),patch.object(agent,'api',side_effect=responses) as api:
            answer=agent.infer(self.wait)['result']
            self.assertEqual(answer['tool_calls'][0]['output'],tool_result)
            self.assertEqual(api.call_count,2)
            self.assertTrue(answer['response_id'].startswith('local-'))
            messages=api.call_args.args[1]['messages']
            self.assertIn({'role':'tool','tool_call_id':'call_1','content':json.dumps(tool_result)},messages)

    def test_tool_transport_failure_does_not_replay_an_edit(self):
        self.cfg.update(inference_api='openai-tools',integrations=[{'allowed_tools':['edit_file']}])
        (self.root/'coding-config.json').write_text(json.dumps(self.cfg))
        responses=[{}, {'tools':[{'name':'edit_file','inputSchema':{'type':'object'}}]}, ConnectionError('response lost after edit')]
        model={'choices':[{'finish_reason':'tool_calls','message':{'role':'assistant','content':None,'tool_calls':[{'id':'edit_1','function':{'name':'edit_file','arguments':'{}'}}]}}]}
        with patch.object(agent,'ROOT',self.root),patch.object(agent,'model_ready'),patch.object(agent,'coding_mcp',side_effect=responses) as rpc,patch.object(agent,'api',return_value=model):
            with self.assertRaises(ConnectionError):agent.infer(self.wait)
            with self.assertRaisesRegex(RuntimeError,'interrupted'):agent.infer(self.wait)
            self.assertEqual(rpc.call_count,3)

    def test_review_continuation_restores_same_workspace_history(self):
        self.cfg.update(inference_api='openai-tools',integrations=[{'allowed_tools':['read_text_file']}])
        (self.root/'coding-config.json').write_text(json.dumps(self.cfg))
        (self.root/'runs').mkdir()
        previous='a'*64
        history=[{'role':'system','content':'old instructions'},{'role':'user','content':'prior task'},{'role':'assistant','content':'prior answer'}]
        (self.root/'runs'/(previous+'.json')).write_text(json.dumps({'state':'completed','response':{'workspace':str(self.workspace),'messages':history}}))
        self.wait['context']['previous_response_id']='local-'+previous
        rpc=[{}, {'tools':[{'name':'read_text_file','inputSchema':{'type':'object'}}]}]
        response={'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':'Reviewed.'}}]}
        with patch.object(agent,'ROOT',self.root),patch.object(agent,'model_ready'),patch.object(agent,'coding_mcp',side_effect=rpc),patch.object(agent,'api',return_value=response) as api:
            agent.infer(self.wait)
            messages=api.call_args.args[1]['messages']
            self.assertEqual(messages[1:3],history[1:])
            self.assertNotEqual(messages[0]['content'],'old instructions')

    def test_review_cannot_import_another_workspace_history(self):
        self.cfg.update(inference_api='openai-tools',integrations=[{'allowed_tools':['read_text_file']}])
        (self.root/'coding-config.json').write_text(json.dumps(self.cfg))
        (self.root/'runs').mkdir()
        previous='b'*64
        (self.root/'runs'/(previous+'.json')).write_text(json.dumps({'state':'completed','response':{'workspace':'/another-project','messages':[]}}))
        self.wait['context']['previous_response_id']='local-'+previous
        rpc=[{}, {'tools':[{'name':'read_text_file','inputSchema':{'type':'object'}}]}]
        with patch.object(agent,'ROOT',self.root),patch.object(agent,'model_ready'),patch.object(agent,'coding_mcp',side_effect=rpc),patch.object(agent,'api') as api:
            with self.assertRaisesRegex(ValueError,'another workspace'):agent.infer(self.wait)
            api.assert_not_called()

    def test_command_uses_same_identity_after_lost_invoke_reply(self):
        self.root.joinpath('commands').mkdir()
        path=self.root/'commands/lmc-test.json'
        path.write_text(json.dumps({'operation':'lmc-test','stage':'binding','lease':'lease1','script':'true'}))
        calls=[]
        def invoke(op,args=None,rid=None):
            calls.append((op,rid))
            if op=='cap.lease':return {'state':'bound'}
            if op=='cap.invoke' and calls.count((op,rid))==1:raise ConnectionError('reply lost')
            if op=='cap.status':return {'state':'succeeded','result':{'exit_code':0,'evidence':{'quiescent':True}}}
            return {}
        with patch.object(bridge,'ROOT',self.root),patch.object(transport,'invoke',invoke,create=True):
            with self.assertRaises(ConnectionError):bridge.command_status({'operation':'lmc-test'})
            result=bridge.command_status({'operation':'lmc-test'})
            self.assertEqual(result['result']['exit_code'],0)
            bridge.command_status({'operation':'lmc-test'})
            self.assertEqual(calls.count(('cap.invoke','lmc-test-invoke')),2)
            self.assertEqual(calls.count(('cap.release','lmc-test-release')),1)

    def test_uncertain_command_keeps_lease_for_reconciliation(self):
        self.root.joinpath('commands').mkdir()
        (self.root/'commands/lmc-test.json').write_text(json.dumps({'operation':'lmc-test','stage':'submitted','lease':'lease1'}))
        invoke=Mock(return_value={'state':'uncertain'})
        with patch.object(bridge,'ROOT',self.root),patch.object(transport,'invoke',invoke,create=True):
            self.assertEqual(bridge.command_status({'operation':'lmc-test'})['state'],'uncertain')
            invoke.assert_called_once_with('cap.status',{'operation':'lmc-test'})

if __name__=='__main__':unittest.main()
