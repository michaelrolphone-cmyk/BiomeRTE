import copy,json,os,subprocess,sys,unittest
from concurrent.futures import ThreadPoolExecutor
import test_delivery as delivery
from biomerte.app import Application
from biomerte import assistant_support as a
from biomerte.api import Context

class CrashAndConcurrency(unittest.TestCase):
    setUp=delivery.Delivery.setUp
    tearDown=delivery.Delivery.tearDown
    execute=delivery.Delivery.execute
    frame=delivery.Delivery.frame
    install=delivery.Delivery.install
    r=delivery.Delivery.r
    def reserved(self):
        self.install();self.r()
        receipt=self.execute(lambda tx:a.admit(tx,self.ctx,tx.need('assistant_responsibilities','r'),'trigger'))
        f=self.execute(lambda tx:tx.need('assistant_cycle_inputs',receipt['cycle_id']))
        return receipt,f
    def child(self,body,*args):
        pre="import sys,json,os;from biomerte.app import Application;from biomerte.api import Context;from biomerte import assistant_support as a\napp=Application(sys.argv[1]);ctx=Context(app);store=app.stores['site']\n"
        return subprocess.run([sys.executable,'-S','-c',pre+body,self.tmp.name,*args],text=True,capture_output=True)
    def reopen(self):
        self.app=Application(self.tmp.name);self.tx=self.app.stores['site'];self.ctx=Context(self.app)
    def test_real_process_crash_before_commit_rolls_back_cycle_and_successor(self):
        receipt,f=self.reserved()
        body="def crash(tx):\n f=tx.need('assistant_cycle_inputs',sys.argv[2]);a.finalize(tx,f,tx.need('assistant_responsibilities','r'),True);os._exit(73)\nstore.execute(crash)\n"
        p=self.child(body,receipt['cycle_id']);self.assertEqual(p.returncode,73,p.stderr)
        self.reopen()
        self.assertEqual(self.execute(lambda tx:tx.rows('assistant_finalizations')),[])
        self.assertEqual(self.execute(lambda tx:tx.rows('assistant_wake_outbox')),[])
        r=self.execute(lambda tx:tx.need('assistant_responsibilities','r'))
        self.assertEqual(r['active_cycle'],receipt['cycle_id']);self.assertTrue(r['cycle_permit'])
        self.execute(lambda tx:a.finalize(tx,f,tx.need('assistant_responsibilities','r'),True))
        self.assertEqual(len(self.execute(lambda tx:tx.rows('assistant_wake_outbox'))),1)
    def test_real_process_crash_after_commit_reuses_receipt_once(self):
        receipt,f=self.reserved()
        p=self.child("store.execute(lambda tx:a.finalize(tx,tx.need('assistant_cycle_inputs',sys.argv[2]),tx.need('assistant_responsibilities','r'),True))\nos._exit(74)\n",receipt['cycle_id'])
        self.assertEqual(p.returncode,74,p.stderr);self.reopen()
        saved=self.execute(lambda tx:tx.need('assistant_finalizations',receipt['cycle_id']))
        replay=self.execute(lambda tx:a.finalize(tx,f,tx.need('assistant_responsibilities','r'),True))
        self.assertEqual(saved,replay)
        self.assertFalse(self.execute(lambda tx:tx.need('assistant_responsibilities','r'))['cycle_permit'])
        self.assertEqual(len(self.execute(lambda tx:tx.rows('assistant_wake_outbox'))),1)
    def test_lost_materialization_response_retains_original_run(self):
        receipt,f=self.reserved()
        body="def deliver(tx):\n f=tx.need('assistant_cycle_inputs',sys.argv[2]);f['admission']={'cycle_id':sys.argv[2]};return a.protocol_stage(tx,ctx,f,'cycle_admission','deliver')\nstore.execute(deliver)\nos._exit(75)\n"
        p=self.child(body,receipt['cycle_id']);self.assertEqual(p.returncode,75,p.stderr);self.reopen()
        saved=self.execute(lambda tx:tx.need('assistant_materialized',receipt['cycle_id']))
        f['admission']=receipt
        replay=self.execute(lambda tx:a.protocol_stage(tx,self.ctx,f,'cycle_admission','deliver'))
        self.assertEqual(replay['frame']['admission_receipt'],saved)
        runs=self.execute(lambda tx:[x for x in tx.rows('runs') if x['name']=='assistant.lifecycle'])
        self.assertEqual([x['id'] for x in runs],[saved['run_id']])
    def test_simultaneous_native_admissions_have_one_cycle_permit(self):
        self.install();self.r()
        body="r=app.call('assistant.control',{'id':'r','command':'resume','command_id':sys.argv[2]},request_id=sys.argv[2]);print(json.dumps(r))\n"
        with ThreadPoolExecutor(max_workers=6) as pool:
            results=list(pool.map(lambda n:self.child(body,'simultaneous-'+str(n)),range(6)))
        for p in results:self.assertEqual(p.returncode,0,p.stderr)
        self.reopen()
        rows=self.execute(lambda tx:tx.rows('assistant_start_outbox'))
        self.assertEqual(len(rows),1)
        r=self.execute(lambda tx:tx.need('assistant_responsibilities','r'))
        self.assertTrue(r['cycle_permit']);self.assertEqual(r['active_cycle'],rows[0]['id'])
        self.assertEqual(len(r['pending_triggers']),5)
    def test_stop_preserves_claimed_operation_and_allows_its_result(self):
        self.r()
        self.app.call('cap.define',{'name':'qualification.ongoing','inputs':{},'outputs':{'done':'bool'}},'contract')
        self.app.call('cap.offer',{'provider':'probe','contract':'qualification.ongoing:1','resource':'qualification-device'},'offer')
        lease=self.app.call('cap.acquire',{'task':'qualification','contract':'qualification.ongoing:1'},'acquire')
        self.app.call('cap.fenced',{'provider':'probe','lease':lease['id'],'generation':lease['generation']},'fence')
        self.app.call('cap.invoke',{'lease':lease['id'],'inputs':{},'operation':'ongoing'},'invoke')
        claim={'provider':'probe','operation':'ongoing','generation':lease['generation']}
        self.app.call('provider.claim',claim,'claim')
        self.app.call('assistant.control',{'id':'r','command':'stop','command_id':'stop'},'stop')
        self.assertEqual(self.app.call('cap.status',{'operation':'ongoing'})['state'],'claimed')
        denied=self.execute(lambda tx:a.admit(tx,self.ctx,tx.need('assistant_responsibilities','r'),'after-stop'))
        self.assertEqual(denied['state'],'stop')
        self.app.call('provider.result',dict(claim,state='succeeded',result={'done':True}),'late-result')
        self.assertEqual(self.app.call('cap.status',{'operation':'ongoing'})['result'],{'done':True})
        self.assertEqual(self.execute(lambda tx:tx.rows('assistant_start_outbox')),[])

    def test_all_terminal_outcomes_retain_evidence_and_deliver_learning_once(self):
        self.install();self.r()
        graph={'inputs':{'frame':'obj'},'outputs':{'disposition':'str','frame':'obj','result_code':'str','data':'obj','harm_status':'str','evidence_refs':'list','continuation':'obj','origin':'obj'},'start':'end','nodes':{'end':{'type':'end','result':{
            'disposition':{'$':'input.frame.test_outcome'},'frame':{'$':'input.frame'},
            'result_code':{'$':'input.frame.test_label'},'data':{},'harm_status':'none',
            'evidence_refs':{'$':'input.frame.evidence_refs'},'continuation':{},'origin':{}}}}}
        revision=self.app.call('process.publish',{'name':'assistant.qualification_outcome','graph':graph},'outcome-definition')['rev']
        def configure(tx):
            r=tx.need('assistant_responsibilities','r');r.update(episode_definition='assistant.qualification_outcome',episode_revision=revision);tx.put('assistant_responsibilities','r',r)
        self.execute(configure)
        expected={}
        for label,outcome in [('accepted','completed'),('failed','faulted'),('rejected','rejected'),('infeasible','completed'),('no_action','completed')]:
            task='task-'+label;f=self.frame()
            f.update(responsibility_id='r',owner_epoch=1,assignment={'id':task},test_outcome=outcome,test_label=label,evidence_refs=['evidence-'+label],recommendation=label)
            self.execute(lambda tx:tx.put('assistant_assignment',task,{'task':task,'epoch':1}))
            row=self.execute(lambda tx:a.episode_start(tx,self.ctx,{'id':'episode-'+label,'responsibility_id':'r','owner_epoch':1,'task_id':task,'frame':f}))
            expected[row['run_id']]=(label,outcome)
        for _ in range(20):
            self.app.tick('site')
            rows=self.execute(lambda tx:tx.rows('assistant_learning_outbox'))
            if len(rows)==5:break
        self.assertEqual(len(rows),5)
        for row in rows:
            label,outcome=expected[row['id']]
            self.assertEqual(row['outcome']['disposition'],outcome,repr(row))
            self.assertEqual(row['outcome']['result_code'],label)
            self.assertEqual(row['outcome']['evidence_refs'],['evidence-'+label])
            self.assertTrue(row['delivered'])
            run=self.app.call('process.get',{'run':row['run_id']})
            self.assertFalse(run['tokens'][0]['vars']['input']['frame']['procedure_change'])
        ids={x['run_id'] for x in rows}
        self.execute(lambda tx:a.pump(tx,self.ctx))
        self.assertEqual({x['run_id'] for x in self.execute(lambda tx:tx.rows('assistant_learning_outbox'))},ids)

if __name__=='__main__':unittest.main()
