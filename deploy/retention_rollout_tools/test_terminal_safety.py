import os,json,sqlite3,tempfile,unittest,hashlib,fcntl
from unittest.mock import patch
import terminal_safety as s
class SafetyTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.home=self.tmp.name
  self.worker=sqlite3.connect(self.home+'/worker.sqlite');self.worker.execute('CREATE TABLE actions(id TEXT,state TEXT,result TEXT)');self.worker.commit()
  self.db=sqlite3.connect(self.home+'/terminal.sqlite');self.db.execute('CREATE TABLE jobs(id TEXT,state TEXT,result TEXT)');self.db.commit()
  self.snap={'boot_id':'boot','processes':{10:{'pid':10,'ppid':1,'pgid':10,'started':1,'start_ticks':1,'state':'S'}}}
 def tearDown(self):self.worker.close();self.db.close();self.tmp.cleanup()
 def action(self,state='uncertain',result=None):self.worker.execute('INSERT INTO actions VALUES(?,?,?)',('op',state,json.dumps(result or {})));self.worker.commit()
 def job(self,state='finished',quiescent=True):
  self.db.execute('INSERT INTO jobs VALUES(?,?,?)',('op',state,json.dumps({'quiescent':quiescent,'process_identity':{'pid':20,'kernel_identity':{'pid':20,'boot_id':'boot','start_ticks':50}}})));self.db.commit()
 def check(self,system='Linux'):
  with patch.object(s,'process_snapshot',return_value=self.snap):return s.inspect_local(self.home,system,[10],self.snap)
 def test_uncertain_completed_and_quiescent_is_safe(self):self.action();self.job();self.assertTrue(self.check()['quiescent'])
 def test_completed_without_receipt_or_evidence_blocks(self):self.action(state='succeeded');self.assertFalse(self.check()['quiescent'])
 def test_uncertain_missing_receipt_blocks(self):self.action();self.assertFalse(self.check()['quiescent'])
 def test_unfinished_local_job_blocks_even_without_poll_action(self):self.job('running');self.assertFalse(self.check()['quiescent'])
 def test_missing_quiescent_evidence_blocks(self):self.job(quiescent=False);self.assertFalse(self.check()['quiescent'])
 def test_original_pid_still_live_blocks(self):
  self.job();self.snap['processes'][20]={'pid':20,'ppid':1,'pgid':20,'start_ticks':50,'state':'S'};self.assertFalse(self.check()['quiescent'])
 def test_orphan_process_group_blocks(self):
  self.job();self.snap['processes'][21]={'pid':21,'ppid':1,'pgid':20,'start_ticks':60,'state':'S'};self.assertFalse(self.check()['quiescent'])
 def test_reused_linux_pid_is_not_old_execution(self):
  self.job();self.snap['processes'][20]={'pid':20,'ppid':1,'pgid':20,'start_ticks':99,'state':'S'};self.assertTrue(self.check()['quiescent'])
 def test_live_worker_descendant_blocks(self):
  self.snap['processes'][30]={'pid':30,'ppid':10,'pgid':30,'state':'S'};self.assertFalse(self.check()['quiescent'])
 def test_rejected_no_effect_uncertain_has_explicit_proof(self):self.action(result={'evidence':{'executed':False,'quiescent':True}});self.assertTrue(self.check()['quiescent'])
 def legacy_error(self):
  self.action();self.db.execute('INSERT INTO jobs VALUES(?,?,?)',('op','finished',json.dumps({'quiescent':True,'reason':'supervisor_error','process_identity':{'supervisor_pid':99},'exit_code':-1})));self.db.commit()
  self.snap['processes'][10]['service_cgroup']='/system.slice/biomerte-terminal.service'
 def test_legacy_finished_error_safe_only_with_empty_cgroup(self):
  self.legacy_error();before=self.worker.execute('SELECT * FROM actions').fetchall();self.assertTrue(self.check()['quiescent']);self.assertEqual(before,self.worker.execute('SELECT * FROM actions').fetchall())
 def test_legacy_error_orphan_in_cgroup_blocks(self):
  self.legacy_error();self.snap['processes'][30]={'pid':30,'ppid':1,'pgid':30,'state':'S','service_cgroup':'/system.slice/biomerte-terminal.service'};self.assertFalse(self.check()['quiescent'])
 def test_legacy_error_live_supervisor_blocks(self):
  self.legacy_error();self.snap['processes'][99]={'pid':99,'ppid':1,'pgid':99,'state':'S'};self.assertFalse(self.check()['quiescent'])
 def test_legacy_error_missing_cgroup_blocks(self):
  self.legacy_error();self.snap['processes'][10].pop('service_cgroup');self.assertFalse(self.check()['quiescent'])
 def mac_receipt(self,state='finished'):
  directory=self.home+'/terminal-results/'+hashlib.sha256(b'op').hexdigest();os.makedirs(directory)
  saved={'operation':'op','state':state,'started_at':10,'result':{'evidence':{'quiescent':True,'duration_seconds':2,'process_identity':{'pid':20,'pgid':20}}}}
  with open(directory+'/receipt.json','w') as f:json.dump(saved,f)
  with open(directory+'/lock','w'):pass
  return directory
 def test_mac_finished_uncertain_receipt_safe(self):self.action();self.mac_receipt();self.assertTrue(self.check('Darwin')['quiescent'])
 def test_mac_receipt_decodes_binary_json_for_python34(self):
  self.mac_receipt();original=json.loads
  def python34_loads(value,*args,**kwargs):
   if isinstance(value,(bytes,bytearray)):raise TypeError('the JSON object must be str, not bytes')
   return original(value,*args,**kwargs)
  with patch.object(s.json,'loads',side_effect=python34_loads):self.assertTrue(self.check('Darwin')['quiescent'])
 def test_mac_running_receipt_blocks(self):self.mac_receipt('running');self.assertFalse(self.check('Darwin')['quiescent'])
 def test_mac_active_receipt_lock_blocks(self):
  d=self.mac_receipt()
  with open(d+'/lock','rb') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);self.assertFalse(self.check('Darwin')['quiescent'])
 def test_mac_live_process_group_blocks(self):
  self.mac_receipt();self.snap['processes'][21]={'pid':21,'ppid':1,'pgid':20,'started':11,'state':'S'};self.assertFalse(self.check('Darwin')['quiescent'])
 def test_mac_reused_pid_after_completion_not_old_execution(self):
  self.mac_receipt();self.snap['processes'][20]={'pid':20,'ppid':1,'pgid':20,'started':100,'state':'S'};self.assertTrue(self.check('Darwin')['quiescent'])
if __name__=='__main__':unittest.main()
