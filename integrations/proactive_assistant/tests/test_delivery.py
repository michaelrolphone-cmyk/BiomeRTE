import copy,json,sys,tempfile,unittest,hashlib
from pathlib import Path
R=Path(__file__).resolve().parents[1];sys.path.insert(0,str(R.parents[1]))
from biomerte.app import Application,initialize
from biomerte import assistant_support as a,process,providers,authorization
from biomerte.api import Context
from biomerte.store import Fault,digest
from biomerte.assistant_routing import select

CFG={'observation_policy':{'mode':'events_only'},'operator_approved':True,'context_bytes':1000000,'model':'qwen3.5-9b','model_aliases':['qwen3.5-9b'],'protocol_contracts':['agent.lmstudio.delegate:1','human.decision:1'],'required_contracts':['human.decision:1'],'resources':{'workspace':'workspace.mac.test','git':'git.mac.test'},'controller_home':'/isolated/test/home','python_executable':'python3','container_runner_path':'/isolated/runner.py','workspace_template':'linux-basic@1','workspace_templates':['linux-basic@1','esp-idf@2'],'gitlab_url':'https://git.example:8443','systems':[{'id':'builtin.echo','dependencies':[]}],'protected_components':['authorization','governance','execution','supervision'],'trusted_verifier_revision':'known-good','trusted_verifier_component':'verifier'}
class Delivery(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();initialize(self.tmp.name,'test');self.app=Application(self.tmp.name);self.ctx=Context(self.app)
  self.ctx.workflow={'run_id':'test-run','name':'assistant.execution','revision':'test-revision','node':'test-node','token_id':'test-token'}
  self.tx=self.app.stores['site'];self.tx.execute(lambda tx:a.configure(tx,self.ctx,CFG))
 def tearDown(self):self.tmp.cleanup()
 def execute(self,fn):return self.tx.execute(fn)
 def frame(self):return {'context':{},'evidence_refs':[],'findings':[],'goal':{'description':'test','criteria':[{'path':'x','operator':'eq','expected':1}]},'_runtime':copy.deepcopy(self.ctx.workflow),'harm_status':'none'}
 def install(self):
  for name in json.loads((R/'import-order.json').read_text()):
   graph=json.loads((R/'compiled'/(name.split('.')[-1]+'.json')).read_text());self.app.call('process.publish',{'name':name,'graph':graph},'publish-'+name)
 def r(self):
  row={'id':'r','goal':self.frame()['goal'],'binding_revision':self.execute(lambda tx:(tx.get('definitions','assistant.lifecycle') or {'rev':'binding1'})['rev']),'cycle_definition':'assistant.lifecycle','epoch':1,'revision':1,'desired':'run','active_cycle':None,'cycle_permit':False,'coverage':{'kind':'wait'}}
  self.execute(lambda tx:tx.put('assistant_responsibilities','r',row));return row
 def run_to_terminal(self,id,steps=150):
  for _ in range(steps):
   r=self.app.call('process.get',{'run':id})
   if r['state']!='running':return r
   self.app.tick('site')
  return self.app.call('process.get',{'run':id})
 def grant(self,tx,p,id='grant'):
  row={'id':id,'state':'active','proposal_revision':p['revision'],'proposal_digest':digest(p),'allowed_actions':[p['action']],'proposal':p,'scope':'single','request_id':'request','response_id':'response','decision_id':'decision'}
  tx.put('authorization_requests','request',{'id':'request','proposal_digest':digest(p)});tx.put('authorization_grants',id,row);return row
 def test_all_50_import_through_native_engine(self):
  self.install();self.assertEqual(len(self.execute(lambda tx:tx.rows('definitions'))),50)
 def test_gate_implementations_cover_every_business_gateway(self):
  f=self.frame();f.update(responsibility_snapshot={},definition={},wake={},admission={})
  def check(tx):
   for k in a.CAT['gates']:
    p,g=k.split('.')
    if 'disposition' in g:continue
    self.assertIsInstance(select(p,g,f,CFG,tx),int,k)
  self.execute(check)
 def test_admission_duplicate_and_trigger_coalescing(self):
  r=self.r();first=self.execute(lambda tx:a.admit(tx,self.ctx,tx.need('assistant_responsibilities','r'),'trigger'))
  again=self.execute(lambda tx:a.admit(tx,self.ctx,tx.need('assistant_responsibilities','r'),'trigger'))
  another=self.execute(lambda tx:a.admit(tx,self.ctx,tx.need('assistant_responsibilities','r'),'other'))
  self.assertEqual(first,again);self.assertEqual(another['state'],'coalesced');self.assertEqual(len(self.execute(lambda tx:tx.rows('assistant_start_outbox'))),1)
 def test_atomic_finalization_checkpoint_and_successor(self):
  self.r();self.execute(lambda tx:a.admit(tx,self.ctx,tx.need('assistant_responsibilities','r'),'trigger'))
  f=self.execute(lambda tx:tx.rows('assistant_cycle_inputs')[0]);f['expected_revision']=self.execute(lambda tx:tx.need('assistant_responsibilities','r')['revision'])
  receipt=self.execute(lambda tx:a.finalize(tx,f,tx.need('assistant_responsibilities','r'),True))
  twice=self.execute(lambda tx:a.finalize(tx,f,tx.need('assistant_responsibilities','r'),True))
  self.assertEqual(receipt,twice);self.assertEqual(len(self.execute(lambda tx:tx.rows('assistant_wake_outbox'))),1)
  self.assertEqual(self.execute(lambda tx:tx.need('assistant_responsibilities','r'))['checkpoint'],f)
 def test_finalization_transaction_rolls_back_as_a_unit(self):
  self.r();self.execute(lambda tx:a.admit(tx,self.ctx,tx.need('assistant_responsibilities','r'),'trigger'))
  f=self.execute(lambda tx:tx.rows('assistant_cycle_inputs')[0])
  def abort(tx):a.finalize(tx,f,tx.need('assistant_responsibilities','r'),True);raise Fault('simulated crash before commit')
  with self.assertRaises(Fault):self.execute(abort)
  self.assertFalse(self.execute(lambda tx:tx.rows('assistant_finalizations')));self.assertTrue(self.execute(lambda tx:tx.need('assistant_responsibilities','r'))['cycle_permit'])
 def test_restart_reads_same_finalization_receipt(self):
  self.r();self.execute(lambda tx:a.admit(tx,self.ctx,tx.need('assistant_responsibilities','r'),'trigger'));f=self.execute(lambda tx:tx.rows('assistant_cycle_inputs')[0])
  expected=self.execute(lambda tx:a.finalize(tx,f,tx.need('assistant_responsibilities','r'),False));self.app=Application(self.tmp.name);self.tx=self.app.stores['site']
  self.assertEqual(expected,self.execute(lambda tx:tx.need('assistant_finalizations',f['cycle_id'])))
 def test_stale_epoch_and_revision_cannot_finalize(self):
  self.r();self.execute(lambda tx:a.admit(tx,self.ctx,tx.need('assistant_responsibilities','r'),'trigger'));f=self.execute(lambda tx:tx.rows('assistant_cycle_inputs')[0])
  stale=dict(f,owner_epoch=0)
  with self.assertRaises(Fault):self.execute(lambda tx:a.finalize(tx,stale,tx.need('assistant_responsibilities','r'),True))
  with self.assertRaises(Fault):self.execute(lambda tx:a.finalize(tx,dict(f,expected_revision=0),tx.need('assistant_responsibilities','r'),True))
 def test_pause_is_preserved_and_resume_admits_once(self):
  self.r();self.app.call('assistant.control',{'id':'r','command':'pause','command_id':'pause1'},'p')
  result=self.execute(lambda tx:a.admit(tx,self.ctx,tx.need('assistant_responsibilities','r'),'trigger'));self.assertEqual(result['state'],'pause')
  self.app=Application(self.tmp.name);self.tx=self.app.stores['site'];self.assertEqual(self.execute(lambda tx:tx.need('assistant_responsibilities','r'))['desired'],'pause')
  self.app.call('assistant.control',{'id':'r','command':'resume','command_id':'resume1'},'r1');self.app.call('assistant.control',{'id':'r','command':'resume','command_id':'resume1'},'r2')
  self.assertEqual(len(self.execute(lambda tx:tx.rows('assistant_start_outbox'))),1)
 def test_revocation_after_reserve_blocks_dispatch(self):
  f=self.frame();p=a.proposal({'contract':'core.echo:1','resource':'builtin.echo','inputs':{'value':'hello'}},f,'echo')
  def run(tx):
   self.grant(tx,p);authorization.reserve(tx,self.ctx,'grant',p,'op',p['action']);g=tx.need('authorization_grants','grant');g['state']='revoked';tx.put('authorization_grants','grant',g)
   with self.assertRaises(Fault):authorization.dispatch(tx,self.ctx,'grant',p,'op')
   self.assertIsNone(tx.get('actions','op'))
  self.execute(run)
 def test_dispatch_duplicate_has_one_action(self):
  p=a.proposal({'contract':'core.echo:1','resource':'builtin.echo','inputs':{'value':42}},self.frame(),'echo')
  def run(tx):
   self.grant(tx,p);authorization.dispatch(tx,self.ctx,'grant',p,'op');authorization.dispatch(tx,self.ctx,'grant',p,'op');self.assertEqual(len(tx.rows('actions')),1)
  self.execute(run)
 def test_unknown_receipt_is_unresolved_and_never_reissued(self):
  result=self.execute(lambda tx:a.action_status(tx,self.ctx,'missing'));self.assertEqual(result[0],'unresolved');self.assertFalse(self.execute(lambda tx:tx.rows('_submissions')))
 def test_harm_survives_child_outcome_mapping(self):
  f=self.frame();c=a.outcome(dict(f,harm_status='detected'),'deferred','recovery_deferred');r=self.execute(lambda tx:a.child(tx,self.ctx,'verification','recover',c,f));self.assertEqual(r['harm_status'],'detected');self.assertEqual(r['frame']['harm_status'],'detected');self.assertEqual(r['disposition'],'deferred')
 def test_reject_preserved_separately_from_defer(self):
  for d in ['rejected','deferred','pending','unresolved','faulted']:
   f=self.frame();r=self.execute(lambda tx:a.child(tx,self.ctx,'project','qualify',a.outcome(f,d,d),f));self.assertEqual(r['frame']['last_disposition'],d)
 def test_model_schema_and_actual_model_are_checked(self):
  schema=a.CAT['runtime_phases']['reasoning']['schema']
  with self.assertRaises(Fault):a.parse_model_result({'result':{'text':'{}','model':'wrong'}},schema,['qwen3.5-9b'])
  with self.assertRaises(Fault):a.parse_model_result({'result':{'text':'{}','model':'qwen3.5-9b'}},schema,['qwen3.5-9b'])
  with self.assertRaises(ValueError):a.parse_model_result({'result':{'text':'```json\n{}\n```','model':'qwen3.5-9b'}},schema,['qwen3.5-9b'])
 def test_context_cannot_discard_permissions_or_evidence_to_fit(self):
  f=self.frame();f['context']={'permissions':'keep','evidence_refs':['x'],'optional_history':'large'}
  r=self.execute(lambda tx:a.local_stage(tx,self.ctx,f,'context','compress'));self.assertEqual(r['frame']['context']['permissions'],'keep');self.assertEqual(r['frame']['context']['evidence_refs'],['x'])
 def test_counter_policies_and_revision_eligibility(self):
  f=self.frame();f.update(local_attempt=5,feedback_round=3,tests_passed=True,tested_revision='r1',candidate_revision='r1',review={'revision':'r2','recommendation':'sign_off'})
  self.execute(lambda tx:self.assertEqual(select('development','attempts',f,CFG,tx),1));self.execute(lambda tx:self.assertEqual(select('development','rounds',f,CFG,tx),1));self.execute(lambda tx:self.assertEqual(select('development','eligible',f,CFG,tx),1))
 def test_internal_ops_reject_direct_untrusted_calls(self):
  with self.assertRaises(Fault):self.execute(lambda tx:a.stage(tx,Context(self.app),'governed','classify',self.frame()))
 def test_native_pending_continuation_survives_restart_without_replay(self):
  self.install();self.app.call('cap.define',{'name':'agent.lmstudio.delegate','inputs':{'request':'str','context':'obj'},'outputs':{'result':'obj'}},'lmdef')
  self.app.call('cap.offer',{'provider':'test-provider','contract':'agent.lmstudio.delegate:1','resource':'test-llm'},'lmoffer')
  run=self.app.call('process.start',{'name':'assistant.architecture','inputs':{'frame':self.frame()}},'run')
  for _ in range(8):self.app.tick('site')
  stored=self.app.call('process.get',{'run':run['id']});self.assertEqual(stored['state'],'running');self.assertTrue(stored['tokens'][0].get('assistant_wait'))
  count=len(self.execute(lambda tx:tx.rows('_submissions')));self.app=Application(self.tmp.name);self.tx=self.app.stores['site']
  for _ in range(5):self.app.tick('site')
  self.assertEqual(len(self.execute(lambda tx:tx.rows('_submissions'))),count);self.assertEqual(self.app.call('process.get',{'run':run['id']})['tokens'][0]['at'],stored['tokens'][0]['at'])
 def test_native_context_workflow_completes_using_internal_reads(self):
  self.install();run=self.app.call('process.start',{'name':'assistant.context','inputs':{'frame':self.frame()}},'context-run');r=self.run_to_terminal(run['id']);self.assertEqual(r['state'],'completed',r);self.assertEqual(r['result']['disposition'],'completed',r)
 def test_source_hash_is_bound_to_tests_and_review(self):
  f=self.frame();files={'main.py':'print(1)'};rev=hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest();f['local_attempt']=0;f['last_result']={'exit_code':0,'stdout':json.dumps({'source_revision':rev,'source_files':files,'tests_passed':True,'commands':[{'exit_code':0}]})}
  r=self.execute(lambda tx:a.local_stage(tx,self.ctx,f,'development','evidence'));self.assertEqual(r['frame']['tested_revision'],rev);self.assertTrue(r['frame']['tests_passed'])
  f['last_result']['stdout']=f['last_result']['stdout'].replace(rev,'bad')
  with self.assertRaises(Fault):self.execute(lambda tx:a.local_stage(tx,self.ctx,f,'development','evidence'))

 def test_native_execution_uses_existing_standing_grant_and_returns_real_result(self):
  self.install();f=self.frame();action={'contract':'core.echo:1','resource':'builtin.echo','inputs':{'value':42}};p=a.proposal(action,f,'Echo test');p['scope']={'kind':'standing','actions':[action]};f['proposal']=p
  def setup(tx):
   g=self.grant(tx,p);g['scope']='standing';tx.put('authorization_grants','grant',g)
  self.execute(setup);run=self.app.call('process.start',{'name':'assistant.execution','inputs':{'frame':f}},'exec-run');result=self.run_to_terminal(run['id'],200)
  self.assertEqual(result['state'],'completed',result);self.assertEqual(result['result']['disposition'],'completed',result);self.assertEqual(result['result']['data'],{'value':42},result)
  self.assertEqual(len(self.execute(lambda tx:tx.rows('actions'))),1)
 def test_native_human_authorization_wait_is_durable_and_never_approves_silence(self):
  self.install();f=self.frame();f['proposal']=a.proposal({'contract':'core.echo:1','resource':'builtin.echo','inputs':{'value':42}},f,'Echo')
  run=self.app.call('process.start',{'name':'assistant.execution','inputs':{'frame':f}},'human-exec')
  for _ in range(80):self.app.tick('site')
  actions=self.execute(lambda tx:tx.rows('actions'));self.assertEqual(len(actions),1);self.assertEqual(actions[0]['contract'],'human.decision:1');self.assertEqual(actions[0]['state'],'claimed')
  self.app=Application(self.tmp.name);self.tx=self.app.stores['site']
  for _ in range(20):self.app.tick('site')
  self.assertEqual(len(self.execute(lambda tx:tx.rows('actions'))),1);self.assertEqual(self.app.call('process.get',{'run':run['id']})['state'],'running')
 def test_pending_model_result_resumes_same_token_and_continues(self):
  self.install();self.app.call('cap.define',{'name':'agent.lmstudio.delegate','inputs':{'request':'str','context':'obj'},'outputs':{'result':'obj'}},'lmdef')
  self.app.call('cap.offer',{'provider':'test-provider','contract':'agent.lmstudio.delegate:1','resource':'test-llm'},'lmoffer')
  f=self.frame();run=self.app.call('process.start',{'name':'assistant.architecture','inputs':{'frame':f}},'phase-run')
  response={'facts':[],'assumptions':[],'findings':['Evidence is limited'],'hypotheses':[],'recommendation':'investigate','proposed_action':None,'candidate_revision':None}
  for _ in range(150):
   self.app.tick('site');box=self.app.call('provider.poll',{'provider':'test-provider'})
   for fence in box['fences']:self.app.call('cap.fenced',{'provider':'test-provider','lease':fence['id'],'generation':fence['generation']},'fence-'+fence['id'])
   for action in box['actions']:
    self.app.call('provider.claim',{'provider':'test-provider','operation':action['id'],'generation':action['generation']},'claim-'+action['id'])
    self.app.call('provider.result',{'provider':'test-provider','operation':action['id'],'generation':action['generation'],'state':'succeeded','result':{'result':{'text':json.dumps(response),'model':'qwen3.5-9b'}}},'result-'+action['id'])
   result=self.app.call('process.get',{'run':run['id']})
   if result['state']!='running':break
  self.assertEqual(result['state'],'completed',result);self.assertEqual(result['result']['disposition'],'completed',result)
  self.assertEqual(len(self.execute(lambda tx:tx.rows('actions'))),6)

 def test_native_idle_cycle_finishes_with_wait_coverage(self):
  self.install();self.r();self.execute(lambda tx:a.admit(tx,self.ctx,tx.need('assistant_responsibilities','r'),'initial'))
  for _ in range(120):
   self.app.tick('site');row=self.execute(lambda tx:tx.need('assistant_responsibilities','r'))
   if row.get('checkpoint'):break
  self.assertIsNone(row['active_cycle'],row);self.assertEqual(row['coverage']['kind'],'wait');self.assertEqual(len(self.execute(lambda tx:tx.rows('assistant_finalizations'))),1)
 def test_wait_keeps_observation_watermark_so_racing_work_is_not_lost(self):
  self.r();self.execute(lambda tx:a.admit(tx,self.ctx,tx.need('assistant_responsibilities','r'),'initial'));f=self.execute(lambda tx:tx.rows('assistant_cycle_inputs')[0]);f['event_cursor']=0
  self.execute(lambda tx:tx.emit('work.created',{'id':'new-task'}));self.execute(lambda tx:a.finalize(tx,f,tx.need('assistant_responsibilities','r'),False));wake=self.execute(lambda tx:tx.rows('assistant_wake_outbox')[0]);self.assertEqual(wake['cursor'],0)

 def test_episode_inputs_resolve_a_pinned_biome_knowledge_revision(self):
  self.install();self.r();f=self.frame();f['assignment']={'id':'task1'}
  inputs={'goal':{'description':'specific task','criteria':[{'path':'x','operator':'eq','expected':1}]},'verification_action':{'contract':'core.echo:1','resource':'builtin.echo','inputs':{'value':1}},'verification_probes':[]}
  rev=self.app.call('knowledge.put',{'key':'assistant.task.task1','body':inputs},'task-inputs')
  def setup(tx):
   r=tx.need('assistant_responsibilities','r');r['episode_definition']='assistant.reasoning';r['episode_revision']=tx.need('definitions','assistant.reasoning')['rev'];tx.put('assistant_responsibilities','r',r);tx.put('assistant_assignment','task1',{'task':'task1','epoch':1})
  self.execute(setup);row=self.execute(lambda tx:a.episode_start(tx,self.ctx,{'id':'episode1','responsibility_id':'r','owner_epoch':1,'task_id':'task1','frame':f}))
  run=self.app.call('process.get',{'run':row['run_id']});actual=run['tokens'][0]['vars']['input']['frame'];self.assertEqual(actual['goal']['description'],'specific task');self.assertIn(rev['rev'],actual['evidence_refs'])

if __name__=='__main__':unittest.main(verbosity=2)
