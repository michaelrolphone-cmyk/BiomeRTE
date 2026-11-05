import sys
import json
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_delivery import Delivery
from biomerte import assistant_support as a
from biomerte.store import digest

class PromptBudget(Delivery):
    def test_large_protocol_observation_is_retained_once(self):
        context={'capabilities':[{'contract':'known:1','description':'x'*15000}],'work':[]}
        f=self.frame();f.update(context=context,last_result=context)
        def run(tx):
            cfg=tx.need('assistant_config','current');cfg['context_bytes']=20000;tx.put('assistant_config','current',cfg)
            tx.put('offers','llm-test',{'contract':'agent.lmstudio.delegate:1','resource':'llm.test','state':'ready'})
            with patch.object(a,'capability',return_value=a.outcome(f,'pending','test')) as call:
                result=a.prompt_phase(tx,self.ctx,f,'test',{'prompt':'Inspect metadata','schema':{}})
                action=call.call_args.args[4]
            self.assertEqual(result['disposition'],'pending')
            sent=action['inputs']['context']
            self.assertEqual(sent['response_schema'],{})
            self.assertEqual(sent['capabilities'],context['capabilities'])
            self.assertEqual(sent['observation'],{'same_as_context_fields':['capabilities','work'],'sha256':digest(context)})
            refs=sent['evidence_refs']
            self.assertEqual(len([r for r in refs if r.startswith('context:')]),1)
            evidence_context={k:v for k,v in sent.items() if k!='evidence_refs'}
            self.assertIn('context:'+digest(evidence_context),refs)
            self.assertTrue(set(refs)<=set(f['evidence_refs']))
            self.assertIn('exact strings from context.evidence_refs',action['inputs']['request'])
        self.execute(run)

    def test_pending_request_is_polled_even_when_later_context_is_oversized(self):
        f=self.frame();f['proposal']={'large':'x'*2000000}
        frozen={'contract':'agent.lmstudio.delegate:1','resource':'llm.test',
                'inputs':{'request':'Original','context':{'evidence_refs':['original']}}}
        f['_operations']={'test':{'operation':'already-submitted','action':frozen}}
        def run(tx):
            with patch.object(a,'capability',return_value=a.outcome(f,'pending','operation_queued')) as call:
                result=a.prompt_phase(tx,self.ctx,f,'test',{'prompt':'New','schema':{}})
                self.assertEqual(call.call_args.args[4],frozen)
            self.assertEqual(result['result_code'],'operation_queued')
        self.execute(run)

    def test_citations_are_checked_against_the_context_actually_sent(self):
        for reference,expected in [('sent','completed'),('context.capabilities','unresolved')]:
            f=self.frame()
            def run(tx):
                tx.put('offers','llm-test',{'contract':'agent.lmstudio.delegate:1','resource':'llm.test','state':'ready'})
                def answer(tx,ctx,frame,key,action,**kwargs):
                    refs=action['inputs']['context']['evidence_refs']
                    parsed={'facts':[{'claim':'Observed metadata','evidence_refs':[refs[-1] if reference=='sent' else reference]}],'assumptions':[],'findings':['Observed metadata'],'hypotheses':[],'recommendation':'investigate','proposed_action':None,'candidate_revision':None}
                    return a.outcome(frame,data={'result':{'model':'qwen3.5-9b','text':json.dumps(parsed)}})
                with patch.object(a,'capability',side_effect=answer):
                    result=a.prompt_phase(tx,self.ctx,f,'discovery.classify',a.CAT['stages']['discovery.classify'])
                self.assertEqual(result['disposition'],expected)
                if expected=='unresolved':self.assertEqual(result['result_code'],'unknown_evidence_reference')
            self.execute(run)

    def test_reviewed_action_schema_survives_optional_registry_trimming(self):
        f=self.frame();f['proposal']={'action':{'contract':'reviewed:1','resource':'terminal.test','inputs':{'argv':['/bin/bash','-c','true']}}}
        required={'id':'reviewed:1','inputs':{'argv':{'type':'list','required':True}},'outputs':{'exit_code':{'type':'int','required':True}}}
        def run(tx):
            cfg=tx.need('assistant_config','current');cfg['context_bytes']=6000;tx.put('assistant_config','current',cfg)
            tx.put('contracts','reviewed:1',required)
            tx.put('contracts','unrelated:1',{'id':'unrelated:1','inputs':{'data':{'type':'str','description':'x'*10000}},'outputs':{}})
            tx.put('offers','llm-test',{'contract':'agent.lmstudio.delegate:1','resource':'llm.test','state':'ready'})
            with patch.object(a,'capability',return_value=a.outcome(f,'pending','test')) as call:
                result=a.prompt_phase(tx,self.ctx,f,'test',{'prompt':'Review command','schema':{}})
                action=call.call_args.args[4]
            sent=action['inputs']['context']
            self.assertEqual(result['disposition'],'pending')
            self.assertEqual(sent['required_tool_schemas'],[required])
            self.assertEqual(sent['tool_schemas'],[])
            self.assertIn('tool_schemas',[r['field'] for r in sent['omitted_refs']])
            self.assertIn('context.required_tool_schemas',action['inputs']['request'])
        self.execute(run)

    def test_oversized_required_action_schema_is_not_silently_dropped(self):
        f=self.frame();f['proposal']={'action':{'contract':'reviewed:1','resource':'terminal.test','inputs':{}}}
        def run(tx):
            cfg=tx.need('assistant_config','current');cfg['context_bytes']=1000;tx.put('assistant_config','current',cfg)
            tx.put('contracts','reviewed:1',{'id':'reviewed:1','inputs':{'data':{'type':'str','description':'x'*2000}},'outputs':{}})
            with patch.object(a,'capability') as call:
                result=a.prompt_phase(tx,self.ctx,f,'test',{'prompt':'Review command','schema':{}})
            call.assert_not_called()
            self.assertEqual(result['result_code'],'essential_context_does_not_fit')
        self.execute(run)

for name in list(dir(Delivery)):
    if name.startswith('test_') and name not in PromptBudget.__dict__:setattr(PromptBudget,name,None)
del Delivery
if __name__=='__main__':unittest.main()
