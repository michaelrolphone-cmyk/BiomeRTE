import importlib.util,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import manager as m
class WorkspaceTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.patch=patch.object(m,'ROOT',Path(self.tmp.name));self.patch.start()
 def tearDown(self):self.patch.stop();self.tmp.cleanup()
 def test_template_revision_immutable(self):
  t={'name':'test','version':'1','base':'debian:bookworm-slim','platform':'linux/arm64','dockerfile':'FROM ${BASE}','probe':'true'}
  a=m.template_publish({'template':t});self.assertEqual(a,m.template_publish({'template':t}))
  with self.assertRaises(ValueError):m.template_publish({'template':dict(t,probe='false')})
 def test_stale_writer_rejected(self):
  w={'state':'active','holder':'new-agent','generation':4}
  for a in ({'holder':'old-agent','generation':3},{'holder':'new-agent','generation':3}):
   with self.assertRaises(ValueError):m.fence(w,a)
  m.fence(w,{'holder':'new-agent','generation':4})
 def test_unresolved_effect_blocks_writer(self):
  with self.assertRaises(RuntimeError):m.fence({'state':'active','holder':'a','generation':1,'active_operation':'uncertain'},{'holder':'a','generation':1})
 def test_ready_template_reuses_without_build(self):
  t={'name':'test','version':'1','base':'debian:bookworm-slim','platform':'linux/arm64','dockerfile':'FROM ${BASE}','probe':'true'}
  t=m.template_publish({'template':t});m.save(m.ROOT/'images'/t['revision']/'state.json',{'state':'ready','image':'sha256:test'})
  with patch.object(m,'exists',return_value=True),patch.object(m,'docker') as d:
   self.assertTrue(m.ensure_image({'template':'test@1'})['reused']);d.assert_not_called()
 def test_corrupt_snapshot_never_restored(self):
  p=m.ROOT/'bad';p.write_bytes(b'corrupt')
  with patch.object(m,'docker') as d:
   with self.assertRaises(RuntimeError):m.restore({'checkpoint':{'volume_archive':{'file':str(p),'sha256':'bad'}}})
   d.assert_not_called()
 def test_completed_run_cleanup_and_active_run_preserved(self):
  w={'id':'ws-test','run_id':'run1','state':'suspended','auto_suspend':True};m.persist(w)
  with patch.object(m,'run_state',return_value={'state':'completed','children':[]}),patch.object(m,'dispose') as dispose:
   m.monitor_once();dispose.assert_called_once()
  with patch.object(m,'run_state',return_value={'state':'running','tokens':[]}),patch.object(m,'dispose') as dispose:
   m.monitor_once();dispose.assert_not_called()
 def test_waiting_run_suspends_only_quiescent(self):
  w={'id':'ws-test','run_id':'run1','state':'active','auto_suspend':True};m.persist(w)
  with patch.object(m,'run_state',return_value={'state':'running','tokens':[{'wait':'human'}]}),patch.object(m,'checkpoint') as cp:
   m.monitor_once();cp.assert_called_once();cp.reset_mock();w['active_operation']='build';m.persist(w);m.monitor_once();cp.assert_not_called()
 def test_identifiers_cannot_escape_storage(self):
  for x in ('../../x','/tmp/x','a/b',''):
   with self.assertRaises(ValueError):m.identifier(x)
if __name__=='__main__':unittest.main()
