import copy,os,tempfile,unittest
from unittest.mock import patch
import mac_independent_services as m
import terminal_safety as s
class IndependentServices(unittest.TestCase):
 def setUp(self):
  self.process={'pid':42,'ppid':1,'pgid':42,'started':100,'state':'S'}
  self.snap={'processes':{10:{'pid':10,'ppid':1,'pgid':10,'started':1,'state':'S'},42:self.process}}
  self.proof=dict(self.process,validated_dynamic_apple=True);self.proof.pop('state')
  self.record={'evidence':{'quiescent':True,'process_identity':{'pid':42,'pgid':42}}}
 def test_exact_independent_process_is_unaffected(self):self.assertIsNone(s.mac_known_process_reason(self.record,self.snap,{42:self.proof}))
 def test_unsigned_proof_blocks(self):
  self.proof['validated_dynamic_apple']=False;self.assertIsNotNone(s.mac_known_process_reason(self.record,self.snap,{42:self.proof}))
 def test_changed_start_blocks(self):
  self.proof['started']=99;self.assertIsNotNone(s.mac_known_process_reason(self.record,self.snap,{42:self.proof}))
 def test_nonquiescent_historical_result_blocks(self):
  self.record['evidence']['quiescent']=False;self.assertIsNotNone(s.mac_known_process_reason(self.record,self.snap,{42:self.proof}))
 def test_extra_group_member_blocks(self):
  self.snap['processes'][43]=dict(self.process,pid=43);self.assertIsNotNone(s.mac_known_process_reason(self.record,self.snap,{42:self.proof}))
 def test_descendant_blocks(self):
  self.process['ppid']=10;self.proof['ppid']=10;self.assertIsNotNone(s.mac_known_process_reason(self.record,self.snap,{42:self.proof}))
 def test_worker_group_blocks_collection(self):
  self.process['pgid']=10;self.assertIsNone(m.proof_for(42,self.snap,{10}))
 def test_root_blocks_collection(self):self.assertIsNone(m.proof_for(42,self.snap,{10,42}))
 def test_non_system_executable_blocks_collection(self):
  with patch.object(m,'executable',return_value='/tmp/Apple-lookalike'):self.assertIsNone(m.proof_for(42,self.snap,{10}))
 def test_verify_changed_proof_fails(self):
  with patch.object(s,'process_snapshot',return_value=self.snap),patch.object(m,'proof_for',return_value=None):self.assertFalse(m.verify({42:self.proof},{10}))

 def test_active_action_cannot_be_exempted(self):
  import sqlite3,json
  with tempfile.TemporaryDirectory() as d:
   db=sqlite3.connect(d+'/worker.sqlite');db.execute('CREATE TABLE actions(state TEXT,result TEXT)')
   for state in ('succeeded','uncertain'):db.execute('INSERT INTO actions VALUES(?,?)',(state,json.dumps(self.record)))
   db.commit();db.close();self.assertEqual(set(),m.candidates(d,self.snap))
 def test_failed_signature_blocks(self):
  import types
  value=types.SimpleNamespace(st_mode=0o100755,st_uid=0,st_dev=1,st_ino=2,st_size=3,st_mtime_ns=4)
  with patch.object(m,'executable',return_value='/System/Library/exact'),patch.object(os.path,'realpath',side_effect=lambda x:x),patch.object(os,'stat',return_value=value),patch.object(m.subprocess,'check_output',return_value=b'501'),patch.object(m,'dynamic_apple_signature',return_value=False):
   self.assertIsNone(m.proof_for(42,self.snap,{10}))
 def test_writable_image_blocks(self):
  import types
  value=types.SimpleNamespace(st_mode=0o100777,st_uid=0)
  with patch.object(m,'executable',return_value='/System/Library/exact'),patch.object(os.path,'realpath',side_effect=lambda x:x),patch.object(os,'stat',return_value=value):
   self.assertIsNone(m.proof_for(42,self.snap,{10}))
 def test_proof_verification_failure_does_not_pass(self):
  with patch.object(s,'process_snapshot',return_value=self.snap),patch.object(m,'proof_for',side_effect=RuntimeError('signature unknown')):
   with self.assertRaises(RuntimeError):m.verify({42:self.proof},{10})
