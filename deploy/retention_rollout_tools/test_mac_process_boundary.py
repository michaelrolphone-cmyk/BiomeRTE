"""Explicit restart-only proof without changing historical effect evidence."""
import copy
import fcntl
import hashlib
import json
import pathlib
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import mac_native as m
import provider_gate as g
import retention_rollout as r
import terminal_safety as s

MODE='process-boundary'


class BoundaryProofTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.home=pathlib.Path(self.tmp.name)
        self.receipts=self.home/'terminal-results';self.receipts.mkdir()
        self.db=sqlite3.connect(str(self.home/'worker.sqlite'))
        self.db.execute('CREATE TABLE actions(id TEXT,signature TEXT,state TEXT,result TEXT,resource TEXT)')
        self.db.execute('INSERT INTO actions VALUES(?,?,?,?,?)',('historical','a'*64,'uncertain','{}','guard'));self.db.commit()
        self.snap={'processes':{10:{'pid':10,'ppid':1,'pgid':10,'started':1,'state':'S'}},'boot_id':None}

    def tearDown(self):self.db.close();self.tmp.cleanup()

    def check(self,fresh=None,mode=MODE,on_fresh=None):
        count=[0]
        def snapshot(system):
            count[0]+=1
            if count[0]==2:
                if on_fresh:on_fresh()
                return fresh or self.snap
            return self.snap
        with patch.object(s,'process_snapshot',side_effect=snapshot):
            out=s.inspect_local(str(self.home),'Darwin',[10],restart_safety_mode=mode)
        if out.get('restart_safe'):self.assertEqual(count[0],2)
        return out

    def result(self,value):
        self.db.execute('UPDATE actions SET result=?',(json.dumps(value),));self.db.commit()

    def receipt(self,state='finished',identity=None):
        d=self.receipts/hashlib.sha256(b'historical').hexdigest();d.mkdir();(d/'lock').touch()
        evidence={'quiescent':False,'duration_seconds':2}
        if identity is not None:evidence['process_identity']=identity
        value={'operation':'historical','state':state,'started_at':10,'result':{'evidence':evidence}}
        (d/'receipt.json').write_text(json.dumps(value));return d,value

    def process(self,pid=20,ppid=1,pgid=20,started=11):
        fresh=copy.deepcopy(self.snap);fresh['processes'][pid]={'pid':pid,'ppid':ppid,'pgid':pgid,'started':started,'state':'S'};return fresh

    def test_missing_history_is_restart_safe_but_not_effect_quiescent(self):
        out=self.check();self.assertTrue(out['restart_safe'],out);self.assertFalse(out['quiescent']);self.assertFalse(out['effects_quiescent'])
        self.assertEqual(out['unresolved_history'][0]['operation'],'historical');self.assertEqual(len(out['evidence_fingerprint']),64)

    def test_default_mode_still_requires_historical_proof(self):self.assertFalse(self.check(mode=None)['quiescent'])

    def test_missing_receipt_directory_is_preserved_missing_history(self):
        self.receipts.rmdir();out=self.check();self.assertTrue(out['restart_safe'],out);self.assertFalse(out['quiescent']);self.assertFalse(self.receipts.exists())

    def test_old_running_receipt_without_identity_remains_unresolved(self):
        self.receipt('running');out=self.check();self.assertTrue(out['restart_safe'],out);self.assertFalse(out['quiescent'])
        self.assertIn('local receipt state running',str(out['unresolved_history']))

    def test_live_or_ambiguous_known_pid_and_group_block(self):
        self.result({'evidence':{'quiescent':False,'process_identity':{'pid':20,'pgid':20}}})
        for fresh in (self.process(pgid=99),self.process(pid=21),self.process(pgid=99,started=100000)):
            self.assertFalse(self.check(fresh)['restart_safe'])

    def test_independently_proven_pid_reuse_does_not_hide_old_group(self):
        self.receipt(identity={'pid':20,'pgid':20})
        fresh=self.process(pgid=99,started=100);self.assertTrue(self.check(fresh)['restart_safe'])
        fresh['processes'][21]={'pid':21,'ppid':1,'pgid':20,'started':11,'state':'S'}
        self.assertFalse(self.check(fresh)['restart_safe'])

    def test_unknown_descendant_and_service_group_orphan_block(self):
        for fresh in (self.process(pid=30,ppid=10,pgid=30),self.process(pid=30,pgid=10)):
            self.assertFalse(self.check(fresh)['restart_safe'])

    def test_worker_identity_change_blocks(self):
        fresh=copy.deepcopy(self.snap);fresh['processes'][10]['started']=20;self.assertFalse(self.check(fresh)['restart_safe'])

    def test_locked_receipt_cannot_be_classified_as_only_history(self):
        directory,_=self.receipt()
        with (directory/'lock').open('rb') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);self.assertFalse(self.check()['restart_safe'])

    def test_every_action_column_is_stable_across_scan(self):
        updates={'id':'changed','signature':'b'*64,'state':'failed','result':'{"new":true}','resource':'new-guard'}
        for column,value in updates.items():
            original=self.db.execute('SELECT '+column+' FROM actions').fetchone()[0]
            def mutate():
                self.db.execute('UPDATE actions SET '+column+'=?',(value,));self.db.commit()
            out=self.check(on_fresh=mutate);self.assertFalse(out['restart_safe'],column)
            self.db.execute('UPDATE actions SET '+column+'=?',(original,));self.db.commit()

    def test_new_action_during_scan_blocks(self):
        def mutate():
            self.db.execute('INSERT INTO actions VALUES(?,?,?,?,?)',('new','c'*64,'failed','{}','other'));self.db.commit()
        self.assertFalse(self.check(on_fresh=mutate)['restart_safe'])

    def test_receipt_content_change_after_second_scan_blocks(self):
        directory,value=self.receipt()
        def mutate():(directory/'receipt.json').write_text(json.dumps(dict(value,state='running')))
        self.assertFalse(self.check(on_fresh=mutate)['restart_safe'])

    def test_receipt_inventory_appearance_during_scan_blocks(self):
        self.assertFalse(self.check(on_fresh=lambda:self.receipt())['restart_safe'])

    def test_acknowledged_outcome_is_resolved_without_fabricating_history(self):
        self.db.execute("UPDATE actions SET state='failed'");self.db.execute('CREATE TABLE result_acknowledged(id TEXT PRIMARY KEY)')
        self.db.execute("INSERT INTO result_acknowledged VALUES('historical')");self.db.commit()
        out=self.check();self.assertTrue(out['restart_safe']);self.assertTrue(out['quiescent']);self.assertEqual(out['unresolved_history'],[])

    def test_no_ledger_or_receipt_mutation(self):
        self.receipt('running');before={str(p):p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        self.assertTrue(self.check()['restart_safe']);self.assertEqual(before,{str(p):p.read_bytes() for p in self.home.rglob('*') if p.is_file()})

    def test_linux_cannot_opt_in(self):
        out=s.inspect_local(str(self.home),'Linux',[10],self.snap,restart_safety_mode=MODE);self.assertFalse(out['restart_safe'])


class BoundaryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.bundle=pathlib.Path(self.tmp.name)
        (self.bundle/'plan.json').write_text(json.dumps({'phases':{'terminal':{'restart_safety_mode':MODE}}}))
        (self.bundle/'gate.json').write_text(json.dumps({'closed':True,'owner_pid':100}))
        self.proof={'restart_safety_mode':MODE,'restart_safe':True,'quiescent':False,'effects_quiescent':False,
                    'unresolved_history':[{'operation':'historical','reason':'missing metadata'}],
                    'evidence_fingerprint':'a'*64,'blocked':[],'checked_receipts':1,'adopted_reconciliations':[]}

    def tearDown(self):self.tmp.cleanup()

    def gate(self,queues):
        class Remote:
            node='provider'
            def call(self,name,args):
                if name!='provider.poll':raise AssertionError('Unexpected write or query '+name)
                return queues.pop(0)
        with patch.object(m.os,'kill'),patch.object(m,'remote',return_value=(Remote(),{})), \
             patch.object(m,'local_safety',return_value=self.proof),patch.object(m,'inspect',return_value={'pid':10}):
            return m.closed_probe(self.bundle)

    def test_closed_probe_does_not_relabel_effect_quiescence(self):
        empty={'actions':[],'fences':[],'activations':[]};out=self.gate([empty,empty])
        self.assertTrue(out['local_restart_safe']);self.assertFalse(out['local_quiescent']);self.assertEqual(out['active_commands'],0)
        self.assertEqual(out['unresolved_history_count'],1)

    def test_closed_probe_rechecks_every_remote_queue_after_local_scan(self):
        empty={'actions':[],'fences':[],'activations':[]}
        for field in empty:
            changed=dict(empty,**{field:[{'id':'new'}]});self.assertGreater(self.gate([empty,changed])['active_commands'],0)

    def test_runner_uses_restart_field_only_under_explicit_mac_mode(self):
        gate={'admission_closed':True,'active_commands':0,'local_quiescent':False,'local_restart_safe':True,'restart_safety_mode':MODE}
        self.assertTrue(r.terminal_gate_ready({'restart_safety_mode':MODE},gate,'Darwin'))
        self.assertFalse(r.terminal_gate_ready({},gate,'Darwin'))
        with self.assertRaises(RuntimeError):r.terminal_gate_ready({'restart_safety_mode':MODE},gate,'Linux')
        self.assertTrue(r.terminal_gate_ready({},dict(gate,local_quiescent=True,local_restart_safe=False),'Linux'))

    def test_runner_never_skips_admission_or_uses_effect_field_as_restart_proof(self):
        gate={'admission_closed':True,'active_commands':0,'local_quiescent':True,'local_restart_safe':True,'restart_safety_mode':MODE}
        for change in ({'admission_closed':False},{'active_commands':1},{'local_restart_safe':False},{'restart_safety_mode':None}):
            self.assertFalse(r.terminal_gate_ready({'restart_safety_mode':MODE},dict(gate,**change),'Darwin'))

    def test_await_uses_explicit_restart_proof_and_requires_inventory_fingerprint(self):
        with patch.object(m,'local_safety',return_value=self.proof),patch.object(m.time,'sleep') as sleep:
            m.await_local_safety(self.bundle);sleep.assert_not_called()
        self.proof['evidence_fingerprint']=None;self.assertFalse(m.restart_ready(self.bundle,self.proof))

    def test_changed_exact_worker_or_watchdog_identity_blocks(self):
        tree={'roots':[10,11],'root':{'pid':10},'worker':{'pid':10},'identities':{10:{'pid':10},11:{'pid':11,'command':'approved'}}}
        changed=copy.deepcopy(tree);changed['identities'][11]['command']='changed'
        with patch.object(m,'inspect',return_value={'pid':10}),patch.object(m,'verified_worker_tree',side_effect=[tree,changed]), \
             patch.object(s,'inspect_local',return_value=self.proof):
            with self.assertRaises(RuntimeError):m.local_safety(self.bundle)

    def test_audit_reports_restart_safe_and_unresolved_history_separately(self):
        snap={'processes':{10:{'pid':10,'ppid':1,'pgid':10},100:{'pid':100,'ppid':1,'pgid':100}}}
        def inspect(service):return {'pid':100 if service=='gui/501/'+m.AUDIT_LABEL else 10}
        with patch.object(m.r,'verified'),patch.object(m.os,'getuid',return_value=501),patch.object(m.os,'getpid',return_value=100), \
             patch.object(m,'inspect',side_effect=inspect),patch.object(m,'verified_worker_tree',return_value={'roots':[10]}), \
             patch.object(s,'process_snapshot',return_value=snap),patch.object(m,'local_safety',return_value=self.proof):
            m.audit_local(self.bundle,'plan')
        out=json.loads((self.bundle/'standalone-audit.json').read_text())
        self.assertTrue(out['restart_safe']);self.assertFalse(out['quiescent']);self.assertFalse(out['effects_quiescent']);self.assertEqual(out['unresolved_history_count'],1)


class StrictGateRegressionTests(unittest.TestCase):
    def test_queued_offline_race_still_restores_ready_before_retry(self):
        class Remote:
            node='provider'
            def __init__(self):self.queues=iter([False,True,False,False]);self.states=[]
            def call(self,name,args):
                if name=='cap.offer':self.states.append(args['state']);return args
                return {'actions':[{'id':'raced'}] if next(self.queues) else [],'fences':[],'activations':[]}
        remote=Remote();offer={'provider':'provider','contract':'terminal:1','resource':'r','service':'terminal','configuration':{},'capacity':16,'state':'ready'}
        with patch.object(g.time,'sleep'):g.acquire(remote,[offer])
        self.assertEqual(remote.states,['offline','ready','offline'])

    def test_strict_gate_does_not_ignore_any_fence_or_activation(self):
        for field in ('actions','fences','activations'):
            queue={'actions':[],'fences':[],'activations':[]};queue[field]=[{'id':'pending'}];self.assertFalse(g.empty(queue))


if __name__=='__main__':unittest.main()
