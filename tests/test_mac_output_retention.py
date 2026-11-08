import hashlib,importlib.util,json,os,pathlib,sys,tempfile,types,unittest
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parents[1]
class MacOutputRetentionTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=pathlib.Path(self.tmp.name)
  fake=types.ModuleType('mac_provider');fake.ROOT=self.root;fake.Remote=object
  terminal=types.ModuleType('terminal');terminal.check_argv=lambda a,b:a;terminal.store_stream=lambda *a:None
  limits=types.ModuleType('terminal_limits');limits.apply_limits=lambda *a:None;limits.read_limits=lambda *a:{}
  with patch.dict(sys.modules,{'mac_provider':fake,'terminal':terminal,'terminal_limits':limits}):
   spec=importlib.util.spec_from_file_location('mac_adapter_retention',ROOT/'providers/mac-terminal/terminal_adapter.py');self.m=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.m)
 def tearDown(self):self.tmp.cleanup()
 def make(self,version=1,state='finished'):
  op='operation';d=self.root/'terminal-results'/hashlib.sha256(op.encode()).hexdigest();d.mkdir(parents=True)
  result={'exit_code':0,'stdout':'secret output','evidence':{'quiescent':True}}
  saved={'operation':op,'signature':'identity','state':state,'result':result}
  if version:saved['retention_version']=version
  self.m.save_receipt(d/'receipt.json',saved);self.m.save_receipt(d/'published.json',result)
  (d/'stdout').write_text('output');(d/'stderr').write_text('');(d/'user-file').write_text('keep')
  return d,{'operation':op,'result_digest':hashlib.sha256(json.dumps(result,sort_keys=True,separators=(',',':')).encode()).hexdigest()}
 def test_ack_keeps_identity_removes_only_copies_and_retries(self):
  d,r=self.make();self.m.acknowledge_result(r);self.m.acknowledge_result(r)
  v=json.loads((d/'receipt.json').read_text());self.assertEqual(v['state'],'acknowledged');self.assertNotIn('result',v);self.assertFalse((d/'stdout').exists());self.assertFalse((d/'published.json').exists());self.assertTrue((d/'user-file').exists())
 def test_legacy_and_unresolved_preserved(self):
  d,r=self.make(version=None);self.assertTrue(self.m.acknowledge_result(r)['legacy_preserved']);self.assertTrue((d/'stdout').exists())
 def test_wrong_digest_does_not_mutate(self):
  d,r=self.make();r['result_digest']='wrong'
  with self.assertRaises(RuntimeError):self.m.acknowledge_result(r)
  self.assertTrue((d/'stdout').exists());self.assertEqual(json.loads((d/'receipt.json').read_text())['state'],'finished')
 def test_unresolved_does_not_mutate(self):
  d,r=self.make(state='running')
  with self.assertRaises(RuntimeError):self.m.acknowledge_result(r)
  self.assertTrue((d/'stdout').exists())
if __name__=='__main__':unittest.main()
