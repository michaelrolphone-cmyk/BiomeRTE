import copy,unittest
import test_stream_telemetry_budget as fixtures
from leased_publish import packet
from biomerte.stream_router import decode
class RoverWire(unittest.TestCase):
 def test_refinement_and_every_raw_sensor_survive_budget_compaction(self):
  value=fixtures.WireBudgetTests().payload()
  refinement={"state":"stationary_refining","segment":3,"session_count":900,"anchor_count":5,"estimate":{"x_m":1,"y_m":2,"z_m":3,"residual_rms_m":1.1,"frame_id":"f","baseline_epoch":4,"baseline_revision":8},"ranges":[{"target_bssid":str(i),"session_count":180,"distance_m":5+i,"std_m":.4} for i in range(5)]}
  value["tof"]["rover_refinement"]=copy.deepcopy(refinement)
  value["tof"]["rover_ranges"]=[{"target_bssid":str(i),"distance_m":5+i,"age_s":1,"samples":12} for i in range(5)]
  result,image=decode(packet(value,b"jpeg")[4:],4194304)
  self.assertEqual(result["tof"]["rover_refinement"]["estimate"],refinement["estimate"])
  self.assertEqual(result["tof"]["rover_refinement"]["session_count"],900)
  self.assertEqual(result["tof"]["rover_ranges"],value["tof"]["rover_ranges"])
  self.assertEqual(result["ahrs"],value["ahrs"])
  self.assertEqual(result["ultrasonic"],value["ultrasonic"])
  self.assertEqual(image,b"jpeg")
