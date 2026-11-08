import importlib.util,json,pathlib,tempfile,unittest
from types import SimpleNamespace
from unittest.mock import patch
if hasattr(importlib.util,'module_from_spec'):
 spec=importlib.util.spec_from_file_location('r',str(pathlib.Path(__file__).with_name('retention_rollout.py')));r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
else:
 import imp
 r=imp.load_source('r',str(pathlib.Path(__file__).with_name('retention_rollout.py')))
class RolloutTests(unittest.TestCase):
 def test_atomic_and_manifest(self):
  with tempfile.TemporaryDirectory() as d:
   p=pathlib.Path(d)/'f';r.write(p,b'abc');self.assertEqual(p.read_bytes(),b'abc');self.assertEqual(p.stat().st_mode&0o777,0o600)
  item={'path':['offer','acknowledge_argv'],'old':None,'old_present':False,'new':['python','terminal.py','--acknowledge']}
  out=json.loads(r.manifest_data(b'{"offer":{"x":1},"other":2}',item).decode('utf-8'));self.assertEqual(out['offer']['x'],1);self.assertEqual(out['other'],2)
  with self.assertRaises(RuntimeError):r.manifest_data(json.dumps(out).encode(),item)
 def run_apply(self,health,old=b'old',expected=None):
  with tempfile.TemporaryDirectory() as d:
   b=pathlib.Path(d);live=b/'live.py';r.write(live,old);r.write(b/'payload',b'new');r.write(b/'qualification.log',b'OK')
   r.dump(b/'qualification.json',{'passed':True,'plan_sha256':'p','hostname':r.socket.gethostname(),'platform':r.platform.system(),'log_sha256':r.digest(b'OK')})
   s={'phases':{'controller':{'restart_argv':['restart'],'health_probe':['health'],'health_timeout':0}},'files':[{'phase':'controller','target':str(live),'payload':'payload','old_sha256':r.digest(old) if expected is None else expected}], 'preserve_probes':{},'preserve_expected':{},'unchanged_files':{}}
   args=SimpleNamespace(bundle=d,plan_sha256='p',phase='controller')
   with patch.object(r,'verified',return_value=(b,s)),patch.object(r,'cmd',return_value=''),patch.object(r,'ready',side_effect=health):
    try:r.apply(args)
    except RuntimeError:pass
   return live.read_bytes(),r.read_json(b/'receipt-controller.json')
 def test_success(self):
  data,receipt=self.run_apply([True,True]);self.assertEqual(data,b'new');self.assertEqual(receipt['state'],'applied')
 def test_failed_health_rolls_back(self):
  data,receipt=self.run_apply([False,False,True,True]);self.assertEqual(data,b'old');self.assertTrue(receipt['rollback_verified'])
 def test_changed_preimage_blocks(self):
  data,receipt=self.run_apply([],expected='incorrect');self.assertEqual(data,b'old');self.assertEqual(receipt['state'],'failed');self.assertFalse(receipt['rollback_verified'])
 def test_paths(self):
  with tempfile.TemporaryDirectory() as d:
   with self.assertRaises(RuntimeError):r.confined(pathlib.Path(d),'../escape')
if __name__=='__main__':unittest.main()

class GateTests(unittest.TestCase):
 def test_race_restores_then_retries(self):
  import provider_gate as g
  class Remote:
   node='provider'
   def __init__(self):self.states=[];self.polls=iter([False,True,False,False])
   def call(self,name,args):
    if name=='cap.offer':self.states.append(args['state']);return args
    pending=next(self.polls);return {'actions':[{}] if pending else [],'fences':[],'activations':[]}
  remote=Remote();offer={'provider':'provider','contract':'x:1','resource':'r','service':'s','configuration':{},'capacity':16,'state':'ready'}
  with patch.object(g.time,'sleep'):
   self.assertTrue(g.acquire(remote,[offer])['admission_closed'])
  self.assertEqual(remote.states,['offline','ready','offline']);g.restore(remote,[offer]);self.assertEqual(remote.states[-1],'ready')
 def test_incomplete_poll_blocks(self):
  import provider_gate as g
  class Remote:
   node='provider'
   def call(self,*args):return {'actions':[]}
  with self.assertRaises(RuntimeError):g.poll(Remote())

class ConfigRolloutTests(unittest.TestCase):
 def exercise(self,health):
  with tempfile.TemporaryDirectory() as d:
   b=pathlib.Path(d);node=b/'node.json';original=b'{"gateway_journal":{"controller_id":"pi","checkpoint_mode":"off"},"other":{"unchanged":7}}'
   r.write(node,original);r.write(b/'qualification.log',b'OK');r.dump(b/'qualification.json',{'passed':True,'plan_sha256':'p','hostname':r.socket.gethostname(),'platform':r.platform.system(),'log_sha256':r.digest(b'OK')})
   s={'phases':{'gatewaylink':{'restart_argv':['restart'],'health_probe':['health'],'health_timeout':0}},'files':[], 'config_edits':[{'phase':'gatewaylink','target':str(node),'old_sha256':r.digest(original),'path':['gateway_journal','checkpoint_mode'],'old_present':True,'old':'off','new':'bootstrap'}], 'preserve_probes':{},'preserve_expected':{},'unchanged_files':{str(node):r.digest(original)}}
   a=SimpleNamespace(bundle=d,plan_sha256='p',phase='gatewaylink')
   with patch.object(r,'verified',return_value=(b,s)),patch.object(r,'cmd',return_value=''),patch.object(r,'ready',side_effect=health):
    try:r.apply(a)
    except RuntimeError:pass
   receipt=r.read_json(b/'receipt-gatewaylink.json');out=json.loads(node.read_text())
   self.assertEqual(out['other'],{'unchanged':7});self.assertEqual(out['gateway_journal']['controller_id'],'pi')
   if receipt['state']=='applied':self.assertEqual(r.completed_baselines(b,s,'p')[0][str(node)],r.digest(node.read_bytes()))
   return out,receipt
 def test_bootstrap_updates_only_checkpoint_mode(self):
  out,receipt=self.exercise([True,True]);self.assertEqual(out['gateway_journal']['checkpoint_mode'],'bootstrap');self.assertEqual(receipt['state'],'applied')
 def test_config_rollback_verifies_original_hash(self):
  out,receipt=self.exercise([False,False,True,True]);self.assertEqual(out['gateway_journal']['checkpoint_mode'],'off');self.assertTrue(receipt['rollback_verified'])
 def test_only_planned_gateway_probe_may_change(self):
  self.assertTrue(r.preserved_equal({'gateway':'1','voice':'a'},{'gateway':'2','voice':'a'},'gatewaylink'))
  self.assertFalse(r.preserved_equal({'gateway':'1','voice':'a'},{'gateway':'2','voice':'b'},'gatewaylink'))
  self.assertFalse(r.preserved_equal({'gateway':'1'},{'gateway':'2'},'controller'))

class MacGatewayHealthTests(unittest.TestCase):
 def test_forwarding_requires_local_node_not_failover(self):
  import mac_native as m
  import io
  class Remote:
   def __init__(self,node):self.node=node
   def call(self,name,args):
    assert name=='system.info' and args=={}
    return {'node':self.node,'site':'site'}
  for remote_node,expected in [('mac',True),('pi',False)]:
   with tempfile.TemporaryDirectory() as d:
    b=pathlib.Path(d);r.dump(b/'before-gatewaylink.json',{'pid':1,'time':1})
    original_read=m.r.read_json
    def read(p):
     if str(p).endswith('/biomerte-controller/node.json'):return {'node':'mac','site':'site'}
     return original_read(p)
    with patch.object(m,'inspect',return_value={'pid':2}),patch.object(m.r,'read_json',side_effect=read),patch.object(m,'remote',return_value=(Remote(remote_node),{})),patch.object(m.urllib.request,'urlopen',return_value=io.BytesIO(b'{"realms":{"site":"ready","local":"ready"}}')):
     self.assertEqual(m.health(b,'gatewaylink')['ready'],expected)
 def test_snapshot_checks_actual_executed_link_path(self):
  import mac_native as m
  import plistlib
  with tempfile.TemporaryDirectory() as d:
   b=pathlib.Path(d);plist=b/'service.plist'
   r.write(plist,plistlib.dumps({'Label':'org.biomerte.controller-link','KeepAlive':True,'ProgramArguments':['python',m.LINK_AGENT],'WorkingDirectory':str(pathlib.Path(m.LINK_AGENT).parent.parent)}))
   with patch.object(m,'inspect',return_value={'pid':3,'path':str(plist)}),patch.object(m.subprocess,'check_output',return_value=('501 START python '+m.LINK_AGENT).encode()):
    m.snapshot(b,'gatewaylink')
   self.assertEqual(r.read_json(b/'before-gatewaylink.json')['pid'],3)


class BundlePathTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=str(pathlib.Path(self.tmp.name).resolve());self.old=self.root+'-old'
 def spec(self):return {'native_bundle':self.root,'helper_hashes':{'mac_native.py':'h'},'phases':{'terminal':{'health_probe':['python',self.root+'/mac_native.py','--bundle',self.root]}}}
 def test_current_paths_pass(self):r.verify_bundle_paths(self.spec(),self.root)
 def test_old_helper_rejected_before_dispatch(self):
  s=self.spec();s['phases']['terminal']['health_probe'][1]=self.old+'/mac_native.py'
  with self.assertRaises(RuntimeError):r.verify_bundle_paths(s,self.root)
 def test_old_bundle_argument_rejected(self):
  s=self.spec();s['phases']['terminal']['health_probe'][-1]=self.old
  with self.assertRaises(RuntimeError):r.verify_bundle_paths(s,self.root)
 def test_old_native_root_rejected(self):
  s=self.spec();s['native_bundle']=self.old
  with self.assertRaises(RuntimeError):r.verify_bundle_paths(s,self.root)

class ExactPreflightTests(unittest.TestCase):
 def exercise(self,changed=False):
  with tempfile.TemporaryDirectory() as d:
   b=pathlib.Path(d);live=b/'live.py';r.write(live,b'old');r.write(b/'payload',b'new');r.write(b/'qualification.log',b'OK')
   r.dump(b/'qualification.json',{'passed':True,'plan_sha256':'p','hostname':r.socket.gethostname(),'platform':'Darwin','log_sha256':r.digest(b'OK')})
   cfg={'restart_argv':['restart'],'health_probe':['health'],'quiescence_probe':['gate'],'standalone_job':'gui/501/job','stop_argv':[]}
   s={'phases':{'terminal':cfg},'files':[{'phase':'terminal','target':str(live),'payload':'payload','old_sha256':r.digest(b'old')}],'preserve_probes':{},'preserve_expected':{},'unchanged_files':{}}
   gate={'admission_closed':True,'active_commands':0,'local_quiescent':True,'worker_pid':123}
   final=dict(gate);final['worker_pid']=456 if changed else 123
   a=SimpleNamespace(bundle=d,plan_sha256='p',phase='terminal')
   with patch.object(r,'verified',return_value=(b,s)),patch.object(r.platform,'system',return_value='Darwin'),patch.object(r,'cmd',return_value='pid = '+str(r.os.getpid())) as command,patch.object(r,'probe',side_effect=[gate,final]),patch.object(r,'ready') as health:
    if changed:
     with self.assertRaises(RuntimeError):r.apply(a,preflight_only=True)
    else:r.apply(a,preflight_only=True)
    self.assertFalse(health.called);self.assertFalse(any(c[0][0]==['restart'] for c in command.call_args_list))
   self.assertEqual(live.read_bytes(),b'old');self.assertFalse((b/'apply-terminal.lock').exists());self.assertFalse((b/'receipt-terminal.json').exists());self.assertEqual(r.read_json(b/'gate-first-terminal-qualification.json'),gate);self.assertEqual(r.read_json(b/'gate-final-terminal-qualification.json'),final)
   self.assertEqual(r.read_json(b/'receipt-terminal-qualification.json')['state'],'failed' if changed else 'qualified')
 def test_exact_preflight_stops_before_replacement(self):self.exercise()
 def test_changed_proof_remains_blocked_and_retained(self):self.exercise(True)

class ProcessBoundaryRollbackTests(unittest.TestCase):
 def exercise(self,safe):
  with tempfile.TemporaryDirectory() as d:
   b=pathlib.Path(d);live=b/'live.py';r.write(live,b'old');r.write(b/'payload',b'new');r.write(b/'qualification.log',b'OK')
   r.dump(b/'qualification.json',{'passed':True,'plan_sha256':'p','hostname':r.socket.gethostname(),'platform':'Darwin','log_sha256':r.digest(b'OK')})
   cfg={'restart_argv':['restart'],'health_probe':['health'],'quiescence_probe':['gate'],'rollback_gate_argv':['regate'],'standalone_job':'gui/501/job','stop_argv':[],'health_timeout':0,'restart_safety_mode':'process-boundary'}
   s={'phases':{'terminal':cfg},'files':[{'phase':'terminal','target':str(live),'payload':'payload','old_sha256':r.digest(b'old')}],'preserve_probes':{},'preserve_expected':{},'unchanged_files':{}}
   gate={'admission_closed':True,'active_commands':0,'local_quiescent':False,'local_restart_safe':True,'restart_safety_mode':'process-boundary','worker_pid':123,'evidence_fingerprint':'a'*64}
   rollback=dict(gate,local_restart_safe=safe)
   a=SimpleNamespace(bundle=d,plan_sha256='p',phase='terminal')
   with patch.object(r,'verified',return_value=(b,s)),patch.object(r.platform,'system',return_value='Darwin'),patch.object(r,'cmd',return_value='pid = '+str(r.os.getpid())),patch.object(r,'probe',side_effect=[gate,gate,rollback]),patch.object(r,'ready',side_effect=[False,False,True,True]):
    with self.assertRaises(RuntimeError):r.apply(a)
   result=r.read_json(b/'receipt-terminal.json');self.assertEqual(result['rollback_verified'],safe);self.assertEqual(live.read_bytes(),b'old' if safe else b'new')
 def test_fresh_restart_safe_proof_allows_rollback_without_reclassifying_history(self):self.exercise(True)
 def test_active_or_ambiguous_execution_still_blocks_rollback(self):self.exercise(False)
