import base64, copy, hashlib, importlib.util, json, pathlib, subprocess, sys, tempfile, unittest
from unittest.mock import patch
R=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(R.parents[1]));sys.path.insert(0,str(R/'tests'))
from test_delivery import Delivery, CFG
from biomerte import assistant_support as a, assistant_workspaces as w
from biomerte.store import Fault, digest
spec=importlib.util.spec_from_file_location('workspace_runner',R.parents[1]/'biomerte/assistant_workspace_runner.py');runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)

class Workspaces(Delivery):
 def setUp(self):
  super().setUp()
  def configure(tx):
   cfg=tx.need('assistant_config','current');cfg['resources'].update(workspace='workspace.mac.test',git='git.mac.test');tx.put('assistant_config','current',cfg)
   tx.put('offers','workspace-offer',{'contract':'workspace.provision:1','resource':'workspace.mac.test','state':'ready'})
  self.execute(configure)
 def f(self):
  f=self.frame();f.update(source_request={'url':'https://git.example:8443/team/repo.git','project':'team/repo','base_commit':'a'*40,'branch':'biome/test'},build_test_recipe={'commands':[['python3','-m','unittest']]})
  f['workspace']={'id':'ws-test','run_id':'test-run','resource':'workspace.mac.test','generation':3,'holder':'episode-test-run','state':'active','template':'linux-basic@1','platform':'linux/arm64'}
  f['workspace_context']=copy.deepcopy(f['workspace']);return f
 def test_workspace_owned_by_episode_not_cycle(self):
  f=self.f();f.pop('workspace');f['cycle_id']='short-cycle'
  def cap(tx,ctx,f,key,action,*args,**kw):
   self.assertEqual(action['inputs']['arguments']['run_id'],'test-run')
   return a.outcome(f,data={'ok':True,'result':{'id':'ws-new','run_id':'test-run','generation':0,'state':'stored'}})
  with patch.object(a,'capability',cap):r=self.execute(lambda tx:w.stage(tx,self.ctx,f,'workspace_open','provision'))
  self.assertEqual(r['frame']['workspace']['resource'],'workspace.mac.test')
 def test_new_cycle_without_workspace_does_not_inherit_old_owner(self):
  f=self.frame();f['workspace_run_id']='old-cycle'
  result=self.execute(lambda tx:w.bind_owner(tx,self.ctx,f))
  self.assertEqual(result['workspace_run_id'],'test-run')
 def test_workspace_offline_waits_without_other_provider(self):
  self.execute(lambda tx:tx.put('offers','workspace-offer',{'contract':'workspace.provision:1','resource':'workspace.mac.test','state':'offline'}))
  r=self.execute(lambda tx:w.stage(tx,self.ctx,self.f(),'workspace_open','provision'))
  self.assertEqual(r['disposition'],'pending');self.assertEqual(r['continuation']['resource'],'workspace.mac.test')
 def test_cross_run_and_stale_holder_rejected(self):
  f=self.f();act=w.action(f,'exec',dict(w.access(f),argv=['true']))
  act['inputs']['arguments']['generation']=2
  with self.assertRaises(Fault):w.guarded_edit(f,act)
  f['workspace']['run_id']='short-cycle'
  with self.assertRaises(Fault):w.access(f)
 def test_handoff_replaces_fence_and_preserves_notes(self):
  f=self.f();f.update(workspace_to_holder='reviewer',workspace_next_action='inspect source')
  def cap(tx,ctx,f,key,action,*args,**kw):
   args=action['inputs']['arguments'];self.assertEqual(args['generation'],3);self.assertEqual(args['context']['next_action'],'inspect source')
   return a.outcome(f,data={'ok':True,'result':dict(f['workspace'],holder='reviewer',generation=5)})
  with patch.object(a,'capability',cap):r=self.execute(lambda tx:w.stage(tx,self.ctx,f,'workspace_transfer','handoff'))
  self.assertEqual(r['frame']['workspace']['generation'],5)
 def test_suspend_resume_retains_identity_refreshes_generation(self):
  f=self.f();steps=[]
  def cap(tx,ctx,f,key,action,*args,**kw):
   steps.append(action['contract']);park=action['contract']=='workspace.suspend:1'
   return a.outcome(f,data={'ok':True,'result':dict(f['workspace'],state='suspended' if park else 'active',holder=None if park else 'episode-test-run',generation=f['workspace']['generation']+1,checkpoint={'id':'cp'})})
  with patch.object(a,'capability',cap):
   r=self.execute(lambda tx:w.stage(tx,self.ctx,f,'workspace_park','suspend'))
   r=self.execute(lambda tx:w.stage(tx,self.ctx,r['frame'],'workspace_resume','acquire'))
  self.assertEqual(steps,['workspace.suspend:1','workspace.acquire:1']);self.assertEqual(r['frame']['workspace']['generation'],5);self.assertEqual(r['frame']['workspace']['id'],'ws-test')
 def test_tests_use_workspace_not_host_bind_mount(self):
  act=w.runner_action(self.f());self.assertEqual(act['contract'],'workspace.exec:1')
  spec=json.loads(act['inputs']['arguments']['argv'][-1]);self.assertEqual(spec['root'],'/workspace/source');self.assertNotIn('image',spec)
 def test_failed_tests_cannot_publish(self):
  f=self.f();f.update(tests_passed=False,tested_revision='x',candidate_revision='x',review={'recommendation':'sign_off','revision':'x'})
  with self.assertRaises(Fault):w.runner_action(f,'commit')
 def test_remote_verification_rejects_different_commit(self):
  f=self.f();f['publication']={'commit':'b'*40,'verified':False}
  with patch.object(a,'capability',lambda tx,ctx,f,*args,**kw:a.outcome(f,data={'ok':True,'result':{'exit_code':0,'stdout':'c'*40+'\trefs/heads/biome/test\n'}})):
   with self.assertRaises(Fault):self.execute(lambda tx:w.stage(tx,self.ctx,f,'git_publish','verify'))
  self.assertFalse(f['publication']['verified'])
 def test_publication_commits_pushes_and_verifies_real_git(self):
  root=pathlib.Path(self.tmp.name)/'repo';root.mkdir();remote=pathlib.Path(self.tmp.name)/'remote.git'
  def git(*argv):return subprocess.run(['git',*argv],cwd=root,capture_output=True,check=True).stdout
  git('init');git('config','user.name','Test');git('config','user.email','test@example.invalid')
  (root/'main.py').write_text('print(1)\n');git('add','.');git('commit','-m','initial')
  subprocess.run(['git','init','--bare',str(remote)],capture_output=True,check=True);git('remote','add','origin',str(remote));git('checkout','-b','biome/test')
  (root/'main.py').write_text('print(2)\n')
  tested=runner.run({'root':str(root),'_test_root':True,'commands':[['python3','main.py']]})
  f=self.f();f.update(tests_passed=True,tested_revision=tested['source_revision'],candidate_revision=tested['source_revision'],review={'recommendation':'sign_off','revision':tested['source_revision']})
  def cap(tx,ctx,f,key,act,*args,**kw):
   inp=act['inputs']['arguments']
   if act['contract']=='workspace.acquire:1':data=f['workspace']
   elif act['contract']=='workspace.exec:1':
    spec=json.loads(inp['argv'][-1]);spec.update(root=str(root),_test_root=True);data={'exit_code':0,'stdout':json.dumps(runner.run(spec))}
   elif act['contract']=='workspace.git:1':data={'exit_code':0,'stdout':git(*inp['argv']).decode()}
   else:raise AssertionError(act)
   return a.outcome(f,data={'ok':True,'result':data})
  with patch.object(a,'capability',cap):
   for stage in ('acquire','commit','push','verify','catalog','review_request'):
    result=self.execute(lambda tx:w.stage(tx,self.ctx,f,'git_publish',stage));self.assertEqual(result['disposition'],'completed');f=result['frame']
  self.assertTrue(f['publication']['verified']);self.assertEqual(f['publication']['source_revision'],tested['source_revision'])
  self.assertEqual(git('ls-remote','origin','refs/heads/biome/test').decode().split()[0],f['publication']['commit'])
 def test_mcp_schema_drift_blocks_call(self):
  f=self.f();f['git_mcp_request']={'tool':'review','arguments':{},'schema_digest':'old'};f['git_mcp_catalog']=[{'name':'review','inputSchema':{'type':'object'}}]
  with self.assertRaises(Fault):self.execute(lambda tx:w.stage(tx,self.ctx,f,'git_publish','review_request'))
 def test_chunked_provider_result_verifies_hash(self):
  raw=json.dumps({'exit_code':0,'stdout':'hello'}).encode();sha=hashlib.sha256(raw).hexdigest()
  self.execute(lambda tx:tx.put('artifacts',sha,{'id':sha,'data':base64.b64encode(raw).decode()}))
  value={'ok':True,'result':{'artifact':{'sha256':sha,'size':len(raw),'chunks':[{'sha256':sha,'size':len(raw)}]}}}
  self.assertEqual(self.execute(lambda tx:w.decode(tx,self.ctx,value))['stdout'],'hello')
  value['result']['artifact']['sha256']='0'*64
  with self.assertRaises(Fault):self.execute(lambda tx:w.decode(tx,self.ctx,value))
 def test_source_pin_rejects_moving_ref_credentials_and_foreign_host(self):
  for field,value in [('base_commit','main'),('url','https://token@git.example:8443/team/repo.git'),('url','https://elsewhere/team/repo.git')]:
   f=self.f();f['source_request'][field]=value
   with self.assertRaises(Fault):self.execute(lambda tx:w.source_request(f,a.configuration(tx)))
 def test_unknown_receipt_preserves_same_operation_and_action(self):
  f=self.f();act=w.action(f,'exec',dict(w.access(f),argv=['true']));f['_operations']={'workspace_open.clone':{'operation':'old-op','action':act}}
  seen=[]
  def cap(tx,ctx,f,key,action,*args,**kw):seen.append(action);return a.outcome(f,'unresolved','unknown',continuation={'operation':'old-op'})
  with patch.object(a,'capability',cap):r,data=self.execute(lambda tx:w.execute(tx,self.ctx,f,'workspace_open.clone',dict(act,contract='changed')))
  self.assertIsNone(data);self.assertEqual(seen,[act]);self.assertEqual(r['continuation']['operation'],'old-op')
 def test_unresolved_workspace_cannot_become_terminal_cleanup(self):
  f=self.f();r=w.hold_unresolved(a.outcome(f,'unresolved','uncertain',continuation={'operation':'original'}))
  self.assertEqual(r['disposition'],'pending');self.assertEqual(r['continuation']['operation'],'original')
  self.assertEqual(r['frame']['workspace_blocked_outcome']['disposition'],'unresolved')
 def test_native_nested_calls_share_parent_workspace_and_resume(self):
  self.install()
  graph={'inputs':{'frame':'obj'},'outputs':{'frame':'obj'},'start':'open','nodes':{
   'open':{'type':'call','process':'assistant.workspace_open','args':{'frame':{'$':'input.frame'}},'save':'opened','next':'park'},
   'park':{'type':'call','process':'assistant.workspace_park','args':{'frame':{'$':'opened.frame'}},'save':'parked','next':'resume'},
   'resume':{'type':'call','process':'assistant.workspace_resume','args':{'frame':{'$':'parked.frame'}},'save':'resumed','next':'end'},
   'end':{'type':'end','result':{'frame':{'$':'resumed.frame'}}}}}
  self.app.call('process.publish',{'name':'assistant.workspace_test','graph':graph},'wrapper')
  f=self.f();f.pop('workspace');f.pop('workspace_context');calls=[];pending=[True];state={}
  def cap(tx,ctx,f,key,act,*args,**kw):
   if key=='workspace_open.provision' and pending[0]:
    pending[0]=False;return a.outcome(f,'pending','provision_wait',continuation={'operation':'same-provision'})
   calls.append(act);args=act['inputs']['arguments'];kind=act['contract']
   if kind=='workspace.provision:1':
    state.update(id='ws-native',run_id=args['run_id'],generation=0,state='stored',holder=None)
   elif kind=='workspace.acquire:1':state.update(generation=state['generation']+1,state='active',holder=args['holder'])
   elif kind=='workspace.suspend:1':state.update(generation=state['generation']+1,state='suspended',holder=None,checkpoint={'id':'cp'})
   else:return a.outcome(f,data={'ok':True,'result':{'exit_code':0,'stdout':json.dumps({'source_revision':'snapshot','source_files':{}}) if kind=='workspace.exec:1' else ''}})
   return a.outcome(f,data={'ok':True,'result':copy.deepcopy(state)})
  with patch.object(a,'capability',cap):
   run=self.app.call('process.start',{'name':'assistant.workspace_test','inputs':{'frame':f}},'wrapper-run')
   result=self.run_to_terminal(run['id'],400)
  self.assertEqual(result['state'],'completed',result)
  self.assertEqual(result['result']['frame']['workspace']['run_id'],run['id'])
  self.assertEqual(result['result']['frame']['workspace']['generation'],3)
  self.assertEqual(sum(x['contract']=='workspace.provision:1' for x in calls),1)

class Runner(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=pathlib.Path(self.tmp.name)/'repo';self.root.mkdir()
  self.g('init');self.g('config','user.name','Test');self.g('config','user.email','test@example.invalid')
  (self.root/'test.py').write_text('print("pass")\n');self.g('add','.');self.g('commit','-m','initial')
 def tearDown(self):self.tmp.cleanup()
 def g(self,*args):return subprocess.run(['git',*args],cwd=self.root,capture_output=True,check=True).stdout
 def test_unlisted_file_changes_digest_and_blocks_commit(self):
  first=runner.snapshot(self.root);(self.root/'new.bin').write_bytes(b'\0\xff')
  self.assertNotEqual(first['source_revision'],runner.snapshot(self.root)['source_revision'])
  with self.assertRaises(ValueError):runner.run({'root':str(self.root),'_test_root':True,'mode':'commit','expected_revision':first['source_revision'],'message':'no'})
 def test_test_failure_is_data_and_source_unchanged(self):
  evidence=runner.run({'root':str(self.root),'_test_root':True,'commands':[['python3','-c','raise SystemExit(3)']]})
  self.assertFalse(evidence['tests_passed']);self.assertEqual(evidence['commands'][0]['exit_code'],3)
  self.assertEqual(evidence['source_revision'],runner.snapshot(self.root)['source_revision'])
 def test_commit_binds_complete_tested_tree(self):
  (self.root/'test.py').write_text('print("updated")\n')
  tested=runner.run({'root':str(self.root),'_test_root':True,'commands':[['python3','test.py']]})
  done=runner.run({'root':str(self.root),'_test_root':True,'mode':'commit','expected_revision':tested['source_revision'],'message':'tested change'})
  self.assertEqual(done['source_revision'],tested['source_revision']);self.assertEqual(done['commit'],self.g('rev-parse','HEAD').decode().strip())
  self.assertEqual(self.g('status','--porcelain'),b'')

# Reuse only the fixture setup, not a duplicated run of every inherited legacy test.
for name in list(vars(Delivery)):
 if name.startswith('test_'):setattr(Workspaces,name,None)
del Delivery
if __name__=='__main__':unittest.main(verbosity=2)
