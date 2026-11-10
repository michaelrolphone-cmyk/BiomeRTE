"""Custody recovery must explain ownership and never free a live executor."""
import tempfile
import unittest
from unittest.mock import patch
from biomerte.app import Application, initialize
from biomerte.store import Fault, uid, digest


class ResourceRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        initialize(self.tmp.name)
        self.app=Application(self.tmp.name)
        self.now=10000.
        self.clock=patch('time.time',side_effect=lambda:self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.call('cap.define',name='host.terminal.run',inputs={},outputs={'ok':'bool'})
    def call(self,op,**args):
        return self.app.call(op,args,uid())
    def start(self):
        self.call('cap.offer',provider='test',contract='host.terminal.run:1',resource='slot',
                  configuration={'execution_custody':True},capacity=1)
        lease=self.call('cap.acquire',task='one',contract='host.terminal.run:1',resource='slot')
        self.call('cap.fenced',provider='test',lease=lease['id'],generation=lease['generation'])
        action=self.call('cap.invoke',lease=lease['id'],operation='one',inputs={})
        self.call('provider.claim',provider='test',operation='one',generation=lease['generation'])
        return lease,action
    def signature(self,action):
        return digest([action[k] for k in ('contract','inputs','resource','generation','lease')])
    def test_timeout_holds_owner_and_queues_conflicting_work(self):
        lease,action=self.start()
        second=self.call('cap.acquire',task='two',contract='host.terminal.run:1',resource='slot')
        self.now+=121
        self.app.tick('site')
        current=self.call('cap.status',operation='one')
        self.assertEqual(current['state'],'uncertain')
        self.assertIn('recovery',current)
        self.assertEqual(self.call('cap.lease',lease=lease['id'])['state'],'bound')
        self.assertEqual(self.call('cap.lease',lease=second['id'])['state'],'queued')
        with self.assertRaises(Fault):
            self.call('cap.release',lease=lease['id'])
        detail=self.call('cap.resource.status',resource='slot')
        self.assertEqual(detail['state'],'held')
        self.assertEqual(detail['owners'][0]['operation'],'one')
        self.assertFalse(detail['owners'][0]['reassignable'])
        self.assertTrue(detail['owners'][0]['next_action'])
    def test_unproved_result_cannot_unlock_supervised_resource(self):
        lease,action=self.start()
        with self.assertRaises(Fault):
            self.call('provider.result',provider='test',operation='one',generation=lease['generation'],
                      state='succeeded',result={'ok':True})
        self.assertEqual(self.call('cap.lease',lease=lease['id'])['state'],'bound')
    def test_runner_completion_releases_and_promotes_queued_work(self):
        lease,action=self.start()
        second=self.call('cap.acquire',task='two',contract='host.terminal.run:1',resource='slot')
        self.call('provider.custody',provider='test',operation='one',generation=lease['generation'],
                  observation={'state':'finished','signature':self.signature(action),'adapter_pid':123})
        self.call('provider.result',provider='test',operation='one',generation=lease['generation'],
                  state='succeeded',result={'ok':True})
        self.assertEqual(self.call('cap.lease',lease=lease['id'])['state'],'released')
        self.assertEqual(self.call('cap.lease',lease=second['id'])['state'],'fencing')
    def test_offline_owner_remains_held_until_verified_reconciliation(self):
        lease,action=self.start()
        self.call('cap.offer',provider='test',contract='host.terminal.run:1',resource='slot',
                  state='offline',configuration={'execution_custody':True})
        self.app.tick('site')
        first=self.call('cap.status',operation='one')
        self.now+=600
        self.app.tick('site')
        self.assertEqual(self.call('cap.lease',lease=lease['id'])['state'],'bound')
        self.assertEqual(self.call('cap.status',operation='one')['recovery'],first['recovery'])
        proof={'identity_digest':self.signature(action),'quiescent':True,'method':'process_identity',
               'evidence':{'boot_id':'observed-boot','process_absent':True}}
        self.call('provider.reconcile',provider='test',operation='one',generation=lease['generation'],
                  state='failed',result={'reason':'Executor stopped; original exit outcome unknown'},proof=proof)
        self.assertEqual(self.call('cap.lease',lease=lease['id'])['state'],'released')
        self.assertEqual(self.call('cap.resource.status',resource='slot')['state'],'free')
    def test_mismatched_proof_preserves_custody(self):
        lease,action=self.start()
        with self.assertRaises(Fault):
            self.call('provider.reconcile',provider='test',operation='one',generation=lease['generation'],
                      state='failed',result={'reason':'stopped'},proof={'identity_digest':'wrong',
                      'quiescent':True,'method':'process_identity','evidence':{'process_absent':True}})
        self.assertEqual(self.call('cap.lease',lease=lease['id'])['state'],'bound')
    def test_reconnected_running_runner_refreshes_liveness(self):
        lease,action=self.start()
        self.now+=121
        self.app.tick('site')
        self.call('provider.custody',provider='test',operation='one',generation=lease['generation'],
                  observation={'state':'running','signature':self.signature(action),'runner_pid':123})
        detail=self.call('cap.recovery.list')['items'][0]
        self.assertEqual(detail['last_confirmed_at'],self.now)
        self.assertEqual(detail['custody']['state'],'running')
        self.assertEqual(self.call('cap.lease',lease=lease['id'])['state'],'bound')
    def test_foreign_custody_signature_is_rejected(self):
        lease,action=self.start()
        with self.assertRaises(Fault):
            self.call('provider.custody',provider='test',operation='one',generation=lease['generation'],
                      observation={'state':'finished','signature':'wrong'})
    def test_fencing_without_execution_can_still_expire_safely(self):
        self.call('cap.offer',provider='test',contract='host.terminal.run:1',resource='slot',
                  configuration={'execution_custody':True})
        lease=self.call('cap.acquire',task='one',contract='host.terminal.run:1',resource='slot')
        self.now+=121
        self.app.tick('site')
        self.assertEqual(self.call('cap.lease',lease=lease['id'])['state'],'released')


if __name__=='__main__':
    unittest.main()
