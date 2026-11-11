import threading
import types
import unittest
import numpy as np
from slam_backend import SlamBackend

class NativeFeatureCountTests(unittest.TestCase):
 def backend(self,keypoints):
  b=object.__new__(SlamBackend);b._lock=threading.RLock();b._state='relocalizing';b._next_frame_index=42
  b._slam=types.SimpleNamespace(tracking=types.SimpleNamespace(f_cur=types.SimpleNamespace(kps=keypoints)),map=types.SimpleNamespace(update_lock=threading.RLock(),num_keyframes=lambda:6,num_points=lambda:46))
  return b
 def test_recovery_counts_real_native_keypoints_without_num_kps_attribute(self):
  b=self.backend(np.zeros((350,2),dtype=np.float32))
  self.assertFalse(hasattr(b._slam.tracking.f_cur,'num_kps'))
  quality=b.continuity_state();self.assertEqual(quality['features'],350);self.assertEqual(quality['state'],'relocalizing');self.assertEqual(quality['frame_index'],42)
 def test_missing_or_empty_keypoints_do_not_trigger_recovery(self):
  for value in (None,np.empty((0,2))):self.assertEqual(self.backend(value).continuity_state()['features'],0)
