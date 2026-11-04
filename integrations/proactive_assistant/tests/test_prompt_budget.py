import sys
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
            self.assertEqual(sent['capabilities'],context['capabilities'])
            self.assertEqual(sent['observation'],{'same_as_context_fields':['capabilities','work'],'sha256':digest(context)})
        self.execute(run)

for name in list(dir(Delivery)):
    if name.startswith('test_') and name not in PromptBudget.__dict__:setattr(PromptBudget,name,None)
del Delivery
if __name__=='__main__':unittest.main()
