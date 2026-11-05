import unittest
import test_delivery as delivery
from biomerte import assistant_support as a

class EvidenceQualification(unittest.TestCase):
    setUp=delivery.Delivery.setUp
    tearDown=delivery.Delivery.tearDown
    execute=delivery.Delivery.execute
    frame=delivery.Delivery.frame
    install=delivery.Delivery.install
    r=delivery.Delivery.r
    def test_engine_fault_before_first_checkpoint_retains_conflicting_evidence(self):
        self.install();self.r()
        graph={'inputs':{'frame':'obj'},'start':'end','nodes':{'end':{'type':'end','result':{'undeclared':True}}}}
        rev=self.app.call('process.publish',{'name':'assistant.qualification_engine_fault','graph':graph},'publish-fault')['rev']
        def setup(tx):
            r=tx.need('assistant_responsibilities','r');r.update(episode_definition='assistant.qualification_engine_fault',episode_revision=rev)
            tx.put('assistant_responsibilities','r',r);tx.put('assistant_assignment','task',{'task':'task','epoch':1})
        self.execute(setup)
        f=self.frame();f.update(responsibility_id='r',owner_epoch=1,assignment={'id':'task'},
            evidence_refs=['observation-before','observation-after'],
            evidence=[{'source':'observation-before','claim':'marker is broken'},{'source':'observation-after','claim':'marker is fixed'}],
            findings=['The observations conflict; current state remains unverified.'],harm_status='suspected')
        row=self.execute(lambda tx:a.episode_start(tx,self.ctx,{'id':'early-fault','responsibility_id':'r','owner_epoch':1,'task_id':'task','frame':f}))
        for _ in range(20):
            self.app.tick('site')
            learned=self.execute(lambda tx:tx.get('assistant_learning_outbox',row['run_id']))
            if learned:break
        self.assertIsNotNone(learned)
        self.assertEqual(learned['outcome']['disposition'],'faulted')
        self.assertEqual(learned['outcome']['result_code'],'engine_fault')
        self.assertEqual(learned['outcome']['evidence_refs'],f['evidence_refs'])
        self.assertEqual(learned['outcome']['frame']['evidence'],f['evidence'])
        self.assertEqual(learned['outcome']['frame']['findings'],f['findings'])
        self.assertEqual(learned['outcome']['harm_status'],'suspected')
        self.assertTrue(learned['delivered'])
        learning=self.app.call('process.get',{'run':learned['run_id']})
        self.assertFalse(learning['tokens'][0]['vars']['input']['frame']['procedure_change'])

if __name__=='__main__':unittest.main()
