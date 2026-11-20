import copy,json,threading,time,unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import numpy as np
from lidar_fusion import defaults,validate_config,zone_points,scale_matches,LidarFusion
from sensor_tick_stream import SensorTickStream
from biomerte.stream_router import Session,frame as wire_frame
from ahrs_source import _calibration,_sensor

class Point:
 def __init__(self,p):self.value=np.array(p,float)
 def is_bad(self):return False
 def pt(self):return self.value.copy()
 def update_position(self,p):self.value=np.array(p)

def scan(distance=1000):
 return {"id":"front","resolution":64,"ready":True,"valid":True,"observed_monotonic_us":1000000,
         "distance_mm":[distance]*64,"target_status":[5]*64,"sequence":1}
def packet(distance=1000):
 return {"schema":"biome.sensor_tick.v1","receiver_id":"rear","boot_id":"boot",
         "frame_id":"boot:1000000","capture_monotonic_us":1000000,"served_monotonic_us":1000010,
         "capture_clock":"esp_timer","image_available":False,"capture_timestamp_semantics":"sensor_snapshot_tick_no_camera_frame",
         "stream_sequence":1,"lidar":{"sensors":[scan(distance)]}}
class LidarTests(unittest.TestCase):
 def test_axial_zone_geometry_and_opposing_mounts(self):
  mounts=defaults()["lidar"];p=zone_points(scan(),mounts["front"],1000000)
  self.assertEqual(64,len(p));self.assertTrue(all(x["xyz_sensor_m"][2]==1 for x in p))
  T=np.array(mounts["rear"]["T_front_camera_sensor_m"])
  rear=T[:3,:3]@p[0]["xyz_sensor_m"]+T[:3,3]
  self.assertLess(rear[2],0);self.assertEqual(-.02,T[1,3])
 def test_reject_invalid_zones_and_future_or_stale_samples(self):
  m=defaults()["lidar"]["front"];s=scan();s["target_status"][0]=255
  self.assertEqual(63,len(zone_points(s,m,1000000)))
  self.assertEqual([],zone_points(s,m,999999));self.assertEqual([],zone_points(s,m,2000001))
  s["distance_mm"][1]=-1;self.assertEqual(62,len(zone_points(s,m,1000000)))
 def test_invalid_rigid_mount_is_rejected(self):
  c=defaults();c["lidar"]["front"]["T_front_camera_sensor_m"][0][0]=2
  with self.assertRaises(ValueError):validate_config(c)
 def test_metric_scale_from_projected_native_landmarks(self):
  mount=defaults()["lidar"]["front"];points=zone_points(scan(),mount,1000000)
  T=np.array(mount["T_front_camera_sensor_m"])
  native=np.array([T[:3,:3]@p["xyz_sensor_m"]+T[:3,3] for p in points])/.5
  values=scale_matches(native,points,mount)
  self.assertGreaterEqual(len(values),40)
  np.testing.assert_allclose([m[1] for m in values],.5)
 def test_native_landmark_depth_fusion_and_anchor_reprojection(self):
  with TemporaryDirectory() as d:
   mount=defaults()["lidar"]["front"];points=zone_points(scan(),mount,1000000);T=np.array(mount["T_front_camera_sensor_m"])
   native=[Point((T[:3,:3]@p["xyz_sensor_m"]+T[:3,3])/.5) for p in points]
   native_map=NS(get_points=lambda:native,update_lock=threading.RLock(),frames=["front-history"])
   backend=NS(_lock=threading.RLock(),_slam=NS(map=native_map),_map_id="map",_last_loop_id=0,
     rig_anchor=lambda pose:{"keyframe_id":"1","T_keyframe_camera":np.eye(4).tolist()},
     _read_entities=lambda:{},_diff=lambda *args:[])
   anchor=np.eye(4);changes=[]
   service=NS(state_dir=Path(d),map_id="map",revision=1,backend=backend,_pipeline_lock=threading.RLock(),
      metric=NS(attachment_status=lambda _:{"availability":"available","scale_m_per_map_unit":.5}),
      _sonar_anchor=lambda *args:{"T_map_keyframe":anchor.tolist()},
      _commit_changes=lambda changes_,reason:changes.append((changes_,reason)))
   fusion=LidarFusion(service);fusion._pose=lambda *args:np.eye(4)
   old=native[12].pt();fusion.process(packet(1050),[10,10.01],"map")
   self.assertGreater(fusion.status()["latest"]["native_landmarks_refined"],0)
   self.assertGreater(native[12].pt()[2],old[2]);self.assertEqual(["front-history"],native_map.frames)
   entities=fusion.entities("map");self.assertGreater(len(entities),40)
   original=entities[0]["data"]["xyz"];anchor[0,3]=3
   self.assertAlmostEqual(original[0]+3,fusion.entities("map")[0]["data"]["xyz"][0])
   self.assertEqual([],fusion.entities("different-map"))
   fusion.close()
 def test_actual_sensor_publication_passes_router_validation(self):
  class Feed:
   def publish(self,metadata,image):self.metadata=metadata;self.image=image
  feed=Feed();service=NS(config={"camera":{"url":"rtsp://127.0.0.1:554/mjpeg/1"}},map_id="map",revision=1,session_id="s",
    _stream_ahrs=lambda f:{},_capture_sensor_stream=lambda *a:{},live_frames=feed)
  stream=SensorTickStream(service);stream.publish(packet(),10,10.01,capture_interval=[9.99,10.])
  metadata=dict(feed.metadata,authority="controller",generation=1,epoch="epoch",sequence=1,type="sensor_tick")
  grant={"id":"s","authority":"controller","generation":1,"epoch":"epoch","contract":"robot.vision.stream:1",
     "lanes":["vision"],"direction":"unidirectional","limits":{"max_message_bytes":100000,"messages_per_second":100,"buffer_depth":1}}
  Session(grant).check("provider","vision",wire_frame(metadata,feed.image))
  del metadata["sensor_packet"]
  with self.assertRaises(Exception):Session(grant).check("provider","vision",wire_frame(metadata,feed.image))
 def test_persistent_calibration_preserves_validation(self):
  row={"status":"succeeded","gyro_bias_valid":True,"gyro_bias_rad_s":[0.,0.,0.],
       "samples":0,"window_ms":2000,"persistent":True,"restored_from_flash":True,"revision":3}
  self.assertTrue(_calibration({"calibration":row})["valid"])
  row["revision"]=None;self.assertFalse(_calibration({"calibration":row})["valid"])
if __name__=="__main__":unittest.main()

class DualFeedTests(unittest.TestCase):
 def test_latest_each_camera_survives_faster_opposite_camera(self):
  from dual_camera_feed import DualCameraFrames,RearCameraFrames
  import struct
  shared=DualCameraFrames();rear=RearCameraFrames(shared)
  with shared.subscribe() as inbox:
   shared.publish({'frame_id':'front-old'},b'a')
   rear.publish({'frame_id':'rear-one'},b'b')
   shared.publish({'frame_id':'front-new'},b'c')
   messages=[]
   for _ in range(2):
    raw=inbox.get(.1);size=struct.unpack('!I',raw[4:8])[0];messages.append(json.loads(raw[8:8+size]))
   self.assertEqual({'front-new','rear-one'},{m['frame_id'] for m in messages})
   self.assertEqual({'front','rear'},{m['camera_id'] for m in messages})

class CausalDepthTests(unittest.TestCase):
 def test_raw_grids_are_immediately_current_before_visual_fusion(self):
  with TemporaryDirectory() as d:
   service=NS(state_dir=Path(d),map_id="map")
   fusion=LidarFusion(service);fusion.enqueue(packet(777),[10.,10.01])
   value=fusion.status()
   self.assertEqual("streaming",value["state"])
   self.assertEqual(777,value["latest"]["sensors"][0]["raw"]["distance_mm"][0])
   self.assertEqual(0,value["processed_scans"])
   fusion.close()
 def test_fusion_waits_for_next_processed_visual_pose_without_blocking_ingress(self):
  with TemporaryDirectory() as d:
   service=NS(state_dir=Path(d),map_id="map",backend=None,camera_rig=NS(config={"rear":{"max_pose_gap_s":.3}}))
   fusion=LidarFusion(service);fusion.visual.append("pending-front-pose")
   ready=threading.Event();timer=threading.Timer(.05,ready.set);timer.start()
   fusion._pose=lambda *args:np.eye(4) if ready.is_set() else None
   start=time.monotonic();fusion.process(packet(),[10.,10.01],"map")
   self.assertGreaterEqual(time.monotonic()-start,.04)
   self.assertEqual(1,fusion.processed);timer.join();fusion.close()
