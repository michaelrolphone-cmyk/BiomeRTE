import copy, unittest
import test_delivery as delivery
CFG=delivery.CFG
from biomerte import assistant_support as a

class ImpactScope(unittest.TestCase):
    setUp=delivery.Delivery.setUp
    tearDown=delivery.Delivery.tearDown
    execute=delivery.Delivery.execute
    frame=delivery.Delivery.frame
    def setup_scope(self):
        cfg=copy.deepcopy(CFG)
        cfg['systems']=[
            {'id':'terminal.target','dependencies':['controller']},
            {'id':'controller','dependencies':['host']},
            {'id':'host','dependencies':[]},
            {'id':'unrelated.agent','dependencies':[],'unknown_relationships':True}]
        self.execute(lambda tx:a.configure(tx,self.ctx,cfg))
        f=self.frame()
        f['proposal']=a.proposal({'contract':'core.echo:1','resource':'terminal.target','inputs':{'value':1}},f,'Qualification target')
        return f
    def test_unrelated_unknown_system_does_not_block_target(self):
        f=self.setup_scope()
        result=self.execute(lambda tx:a.local_stage(tx,self.ctx,f,'inventory','read'))['frame']
        self.assertEqual({x['id'] for x in result['inventory']},{'terminal.target','controller','host'})
        self.assertEqual(result['unknown_dependencies'],[])
    def test_missing_transitive_dependency_is_unresolved(self):
        f=self.setup_scope()
        def change(tx):
            cfg=tx.need('assistant_config','current')
            cfg['systems'][1]['dependencies']=['missing.host']
            tx.put('assistant_config','current',cfg)
        self.execute(change)
        result=self.execute(lambda tx:a.local_stage(tx,self.ctx,f,'inventory','read'))['frame']
        self.assertIn('missing.host',result['unknown_dependencies'])

    def inventory(self, f):
        return self.execute(lambda tx:a.local_stage(tx,self.ctx,f,'inventory','read'))['frame']
    def step(self, f, stage):
        return self.execute(lambda tx:a.local_stage(tx,self.ctx,f,'impact',stage))
    def review_remaining(self, f):
        while f['impact_queue']:
            f['current_system']=copy.deepcopy(f['impact_queue'][0])
            f=self.step(f,'refresh')['frame']
        return f
    def test_all_declared_targets_and_dependency_cycles_are_retained(self):
        f=self.setup_scope()
        f['proposal']['affected_systems'].append('second')
        def change(tx):
            cfg=tx.need('assistant_config','current')
            cfg['systems'] += [{'id':'second','dependencies':['terminal.target']}]
            cfg['systems'][2]['dependencies']=['terminal.target']
            tx.put('assistant_config','current',cfg)
        self.execute(change)
        self.assertEqual({x['id'] for x in self.inventory(f)['inventory']},
                         {'terminal.target','controller','host','second'})
    def test_unknown_target_remains_unresolved(self):
        f=self.setup_scope();f['proposal']['action']['resource']='new.target'
        self.assertIn('new.target',self.inventory(f)['unknown_dependencies'])
    def test_global_inventory_without_action_still_includes_unknowns(self):
        f=self.setup_scope();f.pop('proposal')
        result=self.inventory(f)
        self.assertIn('unrelated.agent',result['unknown_dependencies'])
        self.assertIn('human.decisions',{x['id'] for x in result['inventory']})
    def test_dependency_change_requires_new_reviews(self):
        f=self.step(self.setup_scope(),'queue')['frame']
        f=self.review_remaining(f)
        self.assertTrue(self.step(f,'coverage')['frame']['impact_complete'])
        def change(tx):
            cfg=tx.need('assistant_config','current')
            cfg['systems'][1]['dependencies'].append('new.host')
            cfg['systems'].append({'id':'new.host','dependencies':[]})
            tx.put('assistant_config','current',cfg)
        self.execute(change)
        blocked=self.step(f,'coverage')
        self.assertEqual(blocked['disposition'],'unresolved')
        self.assertEqual(set(blocked['frame']['unreviewed_systems']),{'controller','new.host'})
        f=self.step(f,'refresh')['frame']
        self.assertEqual({x['id'] for x in f['impact_queue']},{'controller','new.host'})
        self.assertTrue(self.step(self.review_remaining(f),'coverage')['frame']['impact_complete'])
    def test_unrelated_inventory_change_does_not_invalidate_completed_reviews(self):
        f=self.review_remaining(self.step(self.setup_scope(),'queue')['frame'])
        def change(tx):
            cfg=tx.need('assistant_config','current')
            cfg['systems'].append({'id':'another.unrelated','dependencies':[],'unknown_relationships':True})
            tx.put('assistant_config','current',cfg)
        self.execute(change)
        self.assertTrue(self.step(f,'coverage')['frame']['impact_complete'])
    def test_discovery_expands_scope_without_certifying_unknown_relationships(self):
        f=self.review_remaining(self.step(self.setup_scope(),'queue')['frame'])
        f['discovered_systems']=[{'id':'newly.discovered','dependencies':[],'unknown_relationships':False}]
        f=self.step(f,'refresh')['frame']
        self.assertIn('newly.discovered',f['unknown_dependencies'])
        self.assertIn('newly.discovered',{x['id'] for x in f['impact_queue']})
        self.assertEqual(self.step(self.review_remaining(f),'coverage')['disposition'],'unresolved')
    def test_pre_upgrade_completed_queue_is_reconciled_without_forged_review(self):
        f=self.setup_scope();f=self.inventory(f)
        f['impact_queue']=copy.deepcopy(f['inventory']);f['impact_index']=len(f['impact_queue'])-1
        f['current_system']=copy.deepcopy(f['impact_queue'][-1])
        f=self.step(f,'refresh')['frame']
        self.assertEqual(f['impact_queue'],[])
        self.assertTrue(self.step(f,'coverage')['frame']['impact_complete'])

if __name__=='__main__': unittest.main(verbosity=2)
