import copy, unittest
import test_delivery as delivery
from biomerte import assistant_support as a
from biomerte.store import Fault

class QualificationBoundaries(unittest.TestCase):
    setUp=delivery.Delivery.setUp
    tearDown=delivery.Delivery.tearDown
    execute=delivery.Delivery.execute
    frame=delivery.Delivery.frame
    install=delivery.Delivery.install
    r=delivery.Delivery.r
    def prepare(self):
        self.install();self.r()
        def seed(tx):
            r=tx.need('assistant_responsibilities','r')
            r['episode_definition']='assistant.reasoning'
            r['episode_revision']=tx.need('definitions','assistant.reasoning')['rev']
            tx.put('assistant_responsibilities','r',r)
            tx.put('assistant_assignment','task1',{'task':'task1','epoch':1})
        self.execute(seed)
        f=self.frame();f.update(responsibility_id='r',owner_epoch=1,assignment={'id':'task1'})
        e=self.execute(lambda tx:a.episode_start(tx,self.ctx,{'id':'episode1','responsibility_id':'r','owner_epoch':1,'task_id':'task1','frame':f}))
        run=self.app.call('process.get',{'run':e['run_id']})
        return run,copy.deepcopy(run['tokens'][0]['vars']['input']['frame'])
    def replacement(self,change=None):
        old=self.execute(lambda tx:tx.need('definitions','assistant.lifecycle')['rev'])
        graph=self.execute(lambda tx:copy.deepcopy(tx.need('process_versions',old)['graph']))
        graph['qualification_revision']='next'
        if change:change(graph)
        return self.app.call('process.publish',{'name':'assistant.lifecycle','graph':graph},'replacement')['rev']
    def adopt(self,rev):
        r=self.execute(lambda tx:tx.need('assistant_responsibilities','r'))
        return self.app.call('assistant.adopt',{'definition':{'id':'r','expected_revision':r['revision'],'binding_revision':rev}},'adopt')
    def test_adoption_preserves_running_episode_epoch_and_pin(self):
        run,f=self.prepare();rev=self.replacement();self.adopt(rev)
        after=self.app.call('process.get',{'run':run['id']})
        self.assertEqual(after['rev'],run['rev'])
        result=self.execute(lambda tx:a.protocol_stage(tx,self.ctx,f,'runtime','validate'))
        self.assertTrue(result['frame']['worker_valid'],'Adoption invalidated a running episode')
        r=self.execute(lambda tx:tx.need('assistant_responsibilities','r'))
        self.assertEqual(r['epoch'],1)
        self.assertEqual(r['binding_revision'],rev)
        admitted=self.execute(lambda tx:a.admit(tx,self.ctx,r,'next-cycle'))
        frame=self.execute(lambda tx:tx.need('assistant_cycle_inputs',admitted['cycle_id']))
        self.assertEqual(frame['binding_revision'],rev)
    def test_adoption_rejects_another_process_definition(self):
        self.prepare()
        rev=self.execute(lambda tx:tx.need('definitions','assistant.execution')['rev'])
        with self.assertRaises(Fault):self.adopt(rev)
    def test_adoption_requires_migration_for_changed_schema(self):
        self.prepare()
        rev=self.replacement(lambda graph:graph['inputs'].update(required_new='str'))
        with self.assertRaises(Fault):self.adopt(rev)
    def test_active_cycle_prevents_adoption(self):
        self.prepare();rev=self.replacement()
        self.execute(lambda tx:a.admit(tx,self.ctx,tx.need('assistant_responsibilities','r'),'active'))
        with self.assertRaises(Fault):self.adopt(rev)
    def test_revoked_scope_prevents_checkpoint_write(self):
        f=self.frame()
        def revoke(tx):
            cfg=tx.need('assistant_config','current');cfg['operator_approved']=False;tx.put('assistant_config','current',cfg)
        self.execute(revoke)
        before=self.execute(lambda tx:tx.rows('assistant_frames'))
        with self.assertRaises(Fault):self.execute(lambda tx:a.local_stage(tx,self.ctx,f,'checkpoint','write'))
        self.assertEqual(before,self.execute(lambda tx:tx.rows('assistant_frames')))
    def test_candidate_cannot_replace_its_own_verifier(self):
        f=self.frame();f.update(changed_components=['governance','verifier'],trusted_verifier_revision='known-good')
        result=self.execute(lambda tx:a.local_stage(tx,self.ctx,f,'change_management','independent'))
        self.assertEqual(result['disposition'],'unresolved')
        self.assertEqual(result['result_code'],'independent_current_verifier_required')
    def test_wrong_verifier_revision_rejected(self):
        f=self.frame();f.update(changed_components=['governance'],trusted_verifier_revision='candidate-verifier')
        result=self.execute(lambda tx:a.local_stage(tx,self.ctx,f,'change_management','independent'))
        self.assertEqual(result['disposition'],'unresolved')
    def test_unchanged_trusted_verifier_remains_usable(self):
        f=self.frame();f.update(changed_components=['governance'],trusted_verifier_revision='known-good')
        result=self.execute(lambda tx:a.local_stage(tx,self.ctx,f,'change_management','independent'))
        self.assertTrue(result['frame']['verifier_unchanged'])
        self.assertEqual(result['disposition'],'completed')

if __name__=='__main__':unittest.main()
