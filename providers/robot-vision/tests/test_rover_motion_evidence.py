import unittest
from rover_motion_evidence import motion_evidence
class MotionEvidenceTests(unittest.TestCase):
 def test_fresh_tracked_pose_is_capture_timed(self):
  e=motion_evidence({"frame_id":"f","receive_decode_monotonic_s":9.5},{"valid":True,"map_id":"m","T_map_camera":[[1,0,0,2],[0,1,0,3],[0,0,1,4],[0,0,0,1]]},wall=100,monotonic=10)
  self.assertTrue(e["tracking"]);self.assertEqual(e["observed_at"],99.5);self.assertEqual(e["position"],[2,3,4]);self.assertFalse(e["metric"])
 def test_stale_invalid_or_missing_pose_is_not_motion_evidence(self):
  for f,l in [({},{}),({"receive_decode_monotonic_s":1},{"valid":True}),({"receive_decode_monotonic_s":10},{"valid":False}),({"receive_decode_monotonic_s":10},{"valid":True,"T_map_camera":[[float("nan")]*4]*4})]:
   self.assertFalse(motion_evidence(f,l,wall=100,monotonic=10)["tracking"])
if __name__=="__main__":unittest.main()
