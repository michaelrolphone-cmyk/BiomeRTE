import copy,json,tempfile,unittest
from pathlib import Path
from tof_source import read_tof
from ultrasonic_source import UltrasonicSource
from stream_telemetry import bounded_metadata,size

class ToFTests(unittest.TestCase):
 def test_source_ages_out_without_position_or_signal_strength(self):
  with tempfile.TemporaryDirectory() as directory:
   path=Path(directory)/"latest.json"
   path.write_text(json.dumps(dict(schema="rover.tof.solution/1",updated_at=100,
    valid=True,status="relative_fix",position=[1,2,3],position_hypotheses=[[1,2,3]],
    anchors=[dict(id="anchor",ranges=[dict(distance_m=2,rssi_dbm=-70)])])))
   fresh=read_tof(path,now=101)
   self.assertTrue(fresh["valid"]);self.assertFalse(fresh["navigation_usable"])
   self.assertNotIn("rssi_dbm",json.dumps(fresh))
   stale=read_tof(path,now=106)
   self.assertFalse(stale["valid"]);self.assertIsNone(stale["position"])
   self.assertEqual(stale["position_hypotheses"],[])
   self.assertEqual(stale["status"],"collector_stale")
   self.assertFalse(read_tof(path,now=90)["valid"])
 def test_large_complete_collector_preserves_survey_and_ranges(self):
  with tempfile.TemporaryDirectory() as directory:
   path=Path(directory)/"latest.json"
   data=dict(schema="rover.tof.solution/1",updated_at=100,valid=True,
             baseline={"valid":True,"ground_plane":{"frame_id":"survey"}},
             rover_ranges=[{"distance_m":2,"details":"x"*300000}])
   path.write_text(json.dumps(data))
   result=read_tof(path,now=101)
   self.assertEqual(result["baseline"],data["baseline"])
   self.assertEqual(result["rover_ranges"],data["rover_ranges"])
 def test_missing_or_malformed_collector_is_unavailable(self):
  with tempfile.TemporaryDirectory() as directory:
   path=Path(directory)/"missing"
   self.assertEqual(read_tof(path)["status"],"collector_unavailable")
   path.write_text('{"schema":"wrong"}')
   self.assertEqual(read_tof(path)["status"],"collector_unavailable")
 def test_stream_keeps_sensor_values_and_all_ftm_details(self):
  value=dict(ahrs={"sensors":{"imu":{"accel_m_s2":[1,2,3],"observed_uptime_ms":100}}},
   ultrasonic={"sensors":[{"id":"front","distance_m":1.2,"observed_uptime_ms":100}]},
   tof={"schema":"rover.tof.solution/1","status":"incomplete","valid":False,
    "anchors":[{"id":str(i),"ranges":[{"distance_m":3,"diagnostic":"x"*800} for j in range(12)]} for i in range(4)]})
  result=bounded_metadata(value)
  self.assertEqual(result,value)
  self.assertEqual(result["ahrs"],value["ahrs"])
  self.assertEqual(result["ultrasonic"],value["ultrasonic"])
  self.assertNotIn("anchor_range_details_compacted",result["tof"])
  self.assertEqual(len(result["tof"]["anchors"][0]["ranges"]),12)
  self.assertEqual(result["tof"]["anchors"][0]["ranges"][0]["distance_m"],3)
  self.assertEqual(len(value["tof"]["anchors"][0]["ranges"]),12)

class SonarReaderTests(unittest.TestCase):
 def test_duplicate_http_reply_does_not_refresh_measurement_age(self):
  clock=[1.]
  envelope=dict(schema=1,receiver_id="camera",boot_id="boot",uptime_ms=1000,sequence=1,
   ultrasonic=dict(schema="hcsr04.v1",paused=False,worker_ready=True,
    sensors=[dict(id=name,valid=True,status="ok",sequence=1,age_ms=100,
      observed_uptime_ms=900,distance_m=1.,echo_us=5882) for name in ["front","left","right","rear"]]))
  class Client:
   network_timeout_s=.2
   def get_ultrasonic(self):return copy.deepcopy(envelope)
  source=UltrasonicSource({"source_id":"camera"},client=Client(),monotonic=lambda:clock[0])
  self.assertTrue(source.poll_once())
  self.assertEqual(source.snapshot()["observations"][0]["effective_age_ms"],100)
  clock[0]=1.5
  self.assertFalse(source.poll_once())
  self.assertEqual(source.snapshot()["observations"][0]["effective_age_ms"],600)
  clock[0]=3.
  self.assertFalse(source.snapshot()["fresh"])
  source.close()
if __name__=="__main__":unittest.main()

class IndependentQueryTests(unittest.TestCase):
 def test_ftm_and_provider_liveness_do_not_touch_metric_or_map_locks(self):
  import threading
  from vision_service import VisionService
  class BusyMap:
   def __getattr__(self,name):raise AssertionError("Map work entered independent query: "+name)
  service=object.__new__(VisionService)
  service._stop=threading.Event();service._lifecycle_state="running"
  service.store=BusyMap();service.metric=BusyMap()
  with tempfile.TemporaryDirectory() as directory:
   service.tof_path=Path(directory)/"missing"
   self.assertEqual(service.query("robot.tof.status")["status"],"collector_unavailable")
   health=service.query("robot.vision.status",{"provider_only":True})
   self.assertEqual(health["state"],"running")
   self.assertIn("robot.tof.status",health["capabilities"])
   self.assertTrue(health["streaming"]["available"])
   with self.assertRaises(ValueError):service.query("robot.tof.status",{"unexpected":True})
