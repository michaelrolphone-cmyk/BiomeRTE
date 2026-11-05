import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_delivery import Delivery
from biomerte import assistant_support as a
from biomerte.store import Fault

class DispatchRecovery(Delivery):
    def seed(self):
        self.r()
        f=self.frame()
        f.update(assignment={'id':'task'},cycle_id='cycle',owner_epoch=1,
                 responsibility_id='r',episode_id='allocated-but-not-created')
        row={'id':'background','responsibility_id':'r','run_id':'dispatch','state':'completed'}
        run={'id':'dispatch','name':'assistant.dispatch','state':'completed',
             'result':a.outcome(f,'faulted','operation_failed')}
        def save(tx):
            tx.put('work','task',{'id':'task','kind':'task','state':'open','priority':1,'sequence':1,'dependencies':[]})
            tx.put('assistant_assignment','task',{'task':'task','epoch':1,'cycle':'cycle'})
            tx.put('assistant_background',row['id'],row)
            tx.put('runs',run['id'],run)
        self.execute(save)
        return f,row,run

    def test_reserved_episode_id_does_not_prove_an_episode_exists(self):
        f,_,_=self.seed()
        self.assertEqual(self.execute(lambda tx:a.release_dispatch_assignment(tx,self.ctx,f)),(True,'released'))
        self.assertIsNone(self.execute(lambda tx:tx.get('assistant_assignment','task')))

    def test_stale_dispatch_cannot_release_replacement(self):
        f,_,_=self.seed()
        self.execute(lambda tx:tx.put('assistant_assignment','task',{'task':'task','epoch':2,'cycle':'new'}))
        self.assertEqual(self.execute(lambda tx:a.release_dispatch_assignment(tx,self.ctx,f)),(False,'superseded_assignment'))

    def test_real_episode_retains_ownership(self):
        f,_,_=self.seed()
        self.execute(lambda tx:tx.put('assistant_episodes','actual',{'id':'actual','task_id':'task','epoch':1}))
        self.assertEqual(self.execute(lambda tx:a.release_dispatch_assignment(tx,self.ctx,f)),(False,'episode_exists'))

    def test_pending_unknown_and_external_failed_effects_retain_reservation(self):
        f,_,_=self.seed();f['operation_id']='operation'
        self.execute(lambda tx:tx.put('_submissions','operation',{'contract':'host.terminal.run:1','inputs':{}}))
        for state in ('pending','unresolved','faulted','completed'):
            with patch.object(a,'action_status',return_value=(state,{})):
                released,reason=self.execute(lambda tx:a.release_dispatch_assignment(tx,self.ctx,f))
            self.assertFalse(released,state)
            self.assertIn('reconciliation',reason)
            self.assertIsNotNone(self.execute(lambda tx:tx.get('assistant_assignment','task')))

    def test_failed_pure_model_request_can_release_reservation(self):
        f,_,_=self.seed();f['_operations']={'architecture.compare':{'operation':'model'}}
        self.execute(lambda tx:tx.put('_submissions','model',{'contract':'agent.lmstudio.delegate:1','inputs':{'context':{'coding':False}}}))
        with patch.object(a,'action_status',return_value=('faulted',{})):
            self.assertEqual(self.execute(lambda tx:a.release_dispatch_assignment(tx,self.ctx,f)),(True,'released'))

    def test_legacy_completed_engine_run_becomes_visible_blocker_once(self):
        self.seed()
        self.execute(lambda tx:a.pump(tx,self.ctx))
        status=self.execute(lambda tx:a.assistant_status(tx,self.ctx,'r'))
        self.assertEqual(status['state'],'blocked')
        self.assertEqual(status['background'][0]['state'],'completed')
        self.assertEqual(status['background'][0]['disposition'],'faulted')
        self.assertTrue(status['blocked_dispatches'][0]['reservation_released'])
        before=self.execute(lambda tx:tx.rows('events'))
        self.execute(lambda tx:a.pump(tx,self.ctx))
        self.assertEqual(before,self.execute(lambda tx:tx.rows('events')))

    def test_blocked_task_does_not_starve_independent_work_or_spin(self):
        self.seed();self.execute(lambda tx:a.pump(tx,self.ctx))
        def choose(tx):
            r=tx.need('assistant_responsibilities','r');r.update(active_cycle='new',cycle_permit=True);tx.put('assistant_responsibilities','r',r)
            tx.put('work','other',{'id':'other','kind':'task','state':'open','priority':0,'sequence':2,'dependencies':[]})
            f=self.frame();f.update(responsibility_id='r',owner_epoch=1,cycle_id='new')
            return a.protocol_stage(tx,self.ctx,f,'planning','reserve')
        result=self.execute(choose)
        self.assertEqual(result['frame']['assignment']['id'],'other')

    def test_explicit_retry_rearms_only_expected_failure(self):
        self.seed();self.execute(lambda tx:a.pump(tx,self.ctx))
        with self.assertRaises(Fault):
            self.execute(lambda tx:a.retry_dispatch(tx,self.ctx,'r','task','stale-run'))
        result=self.execute(lambda tx:a.retry_dispatch(tx,self.ctx,'r','task','dispatch'))
        self.assertEqual(result['state'],'reserved')
        self.assertIsNone(self.execute(lambda tx:tx.get('assistant_dispatch_failures','task')))
        self.execute(lambda tx:a.reconcile_dispatch(tx,self.ctx,tx.need('assistant_background','background'),tx.need('runs','dispatch')))
        self.assertIsNone(self.execute(lambda tx:tx.get('assistant_dispatch_failures','task')))

    def test_retry_refuses_unreconciled_effect(self):
        f,row,run=self.seed();f['operation_id']='effect';run['result']['frame']=f
        def block(tx):
            tx.put('_submissions','effect',{'contract':'host.terminal.run:1','inputs':{}})
            with patch.object(a,'action_status',return_value=('unresolved',{})):
                a.reconcile_dispatch(tx,self.ctx,row,run)
        self.execute(block)
        with self.assertRaisesRegex(Fault,'Reconcile'):
            self.execute(lambda tx:a.retry_dispatch(tx,self.ctx,'r','task','dispatch'))

for name in list(dir(Delivery)):
    if name.startswith('test_') and name not in DispatchRecovery.__dict__:setattr(DispatchRecovery,name,None)
del Delivery
if __name__=='__main__':unittest.main()
