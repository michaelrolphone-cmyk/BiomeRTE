import ast,pathlib,threading,types,unittest
root=pathlib.Path(__file__).resolve().parent
source=(root/'slam_backend.py' if (root/'slam_backend.py').exists() else root.parent/'slam_backend.py').read_text();tree=ast.parse(source)
fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='next_native_frame_index');scope={};exec(compile(ast.Module(body=[fn],type_ignores=[]),'<adapter helper>','exec'),scope);next_index=scope['next_native_frame_index']
class NativeFrameSequence(unittest.TestCase):
 def runtime(self,previous):
  m=types.SimpleNamespace(update_lock=threading.RLock(),previous=previous);m.get_frame=lambda i:m.previous;m.get_keyframes=lambda:[];return types.SimpleNamespace(map=m)
 def test_empty_temporal_history_seeds_existing_keyframe_without_current_pose(self):
  r=self.runtime(None);reference=types.SimpleNamespace(img_id=72,is_bad=lambda:False);r.map.get_keyframes=lambda:[reference];added=[]
  def append(frame):added.append(frame);r.map.previous=frame
  r.map.add_frame=append
  self.assertEqual(next_index(r,500),73);self.assertEqual(added,[reference]);self.assertFalse(hasattr(r,'current_pose'))
 def test_restored_chain_uses_last_accepted_not_burned_counter(self):
  r=self.runtime(types.SimpleNamespace(img_id=108));self.assertEqual(next_index(r,401),109)
 def test_rejected_before_append_retries_same_native_successor(self):
  r=self.runtime(types.SimpleNamespace(img_id=108));n=next_index(r,109)
  self.assertEqual(next_index(r,n+1),109)
  r.map.previous=types.SimpleNamespace(img_id=109);self.assertEqual(next_index(r,110),110)
 def test_exception_after_append_continues_forward(self):
  r=self.runtime(types.SimpleNamespace(img_id=110));self.assertEqual(next_index(r,110),111)
 def test_initialization_keeps_adapter_counter_without_native_reference(self):
  r=self.runtime(None);self.assertEqual(next_index(r,35),35)
if __name__=='__main__':unittest.main()

