import copy,json,pathlib,struct,sys,unittest
sys.path[:0]=[str(pathlib.Path(__file__).resolve().parents[1]),str(pathlib.Path(__file__).resolve().parents[3])]
from leased_publish import packet
from stream_telemetry import bounded_metadata
from biomerte.stream_router import decode
class WireBudgetTests(unittest.TestCase):
 def payload(self):
  return {"frame_id":"bound","detections":[],"pose":{"valid":False},
   "ahrs":{"ahrs":{"worker_ready":True,"paused":False,"imu":{"accel_m_s2":[1,2,3],"observed_uptime_ms":100}}},
   "ultrasonic":{"sensors":[{"id":"front","distance_m":1.2,"observed_uptime_ms":100}]},
   "tof":{"schema":"rover.tof.solution/1","status":"incomplete_geometry","valid":False,
     "anchors":[{"id":str(i),"connected":True,"ranges":[{"rtt_ns":40,"distance_m":6,"diagnostics":"x"*400} for j in range(12)]} for i in range(4)]}}
 def test_large_ftm_detail_decodes_on_actual_wire_without_changing_sensor_samples(self):
  original=self.payload();before=copy.deepcopy(original)
  self.assertGreater(len(json.dumps(original)),16384)
  value=packet(original,b"jpeg");metadata,image=decode(value[4:],4194304)
  self.assertLessEqual(struct.unpack("!I",value[4:8])[0],15872)
  self.assertEqual(metadata["ahrs"],original["ahrs"])
  self.assertEqual(metadata["ultrasonic"],original["ultrasonic"])
  self.assertTrue(metadata["tof"]["anchor_range_details_compacted"])
  self.assertEqual(len(metadata["tof"]["anchors"][0]["ranges"]),12)
  self.assertEqual(metadata["tof"]["anchors"][0]["ranges"][0]["rtt_ns"],40)
  self.assertEqual(metadata["tof"]["detail_capability"],"robot.tof.status")
  self.assertEqual(image,b"jpeg");self.assertEqual(original,before)
 def test_smaller_budget_preserves_range_quality_and_imu_vectors(self):
  value=self.payload();reduced=bounded_metadata(value,8000)
  self.assertLessEqual(len(json.dumps(reduced,separators=(",",":")).encode()),8000)
  self.assertEqual(reduced["tof"]["status"],"incomplete_geometry")
  self.assertFalse(reduced["tof"]["valid"])
  self.assertEqual(reduced["ahrs"]["ahrs"]["imu"]["accel_m_s2"],[1,2,3])
if __name__=="__main__":unittest.main()
