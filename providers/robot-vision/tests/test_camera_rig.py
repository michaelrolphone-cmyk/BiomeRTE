import copy, json, tempfile, threading, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from camera_rig import DEFAULT, DEFAULT_MOUNT, CaptureClock, SharedCameraRig, triangulate_pair, interpolate_pose, validate

CAL={"calibration_id":"rear-test","validated":True,"provenance":"synthetic","width":640,"height":480,
     "fx":300.,"fy":300.,"cx":320.,"cy":240.,"distortion":[0.,0.,0.,0.,0.]}

def metadata(t,boot="rear",offset=100.,jitter=.002):
 host=t+offset
 return {"capture_monotonic_us":int(t*1e6),"capture_host_monotonic_interval_s":[host-jitter,host+jitter],
         "sensor_packet":{"receiver_id":"rear","boot_id":boot}}

class RigGeometryTests(unittest.TestCase):
 def test_camera_selector_contracts_are_versioned_and_rig_configuration_is_mutating(self):
  from biome_provider import CONTRACTS,MUTATING_CONTRACTS
  for name in ("robot.vision.frame","robot.vision.snapshot","robot.vision.geometry"):
   self.assertNotIn("camera_id",CONTRACTS[name+":1"]["inputs"])
   self.assertEqual(CONTRACTS[name+":2"]["inputs"]["camera_id"],"str?")
  self.assertIn("robot.vision.rig.configure:1",MUTATING_CONTRACTS)
  self.assertNotIn("robot.vision.rig.status:1",MUTATING_CONTRACTS)
 def test_mount_is_proper_opposite_camera_at_same_height(self):
  T=np.asarray(DEFAULT_MOUNT);np.testing.assert_allclose(T[:3,:3]@T[:3,:3].T,np.eye(3))
  self.assertAlmostEqual(np.linalg.det(T[:3,:3]),1.)
  np.testing.assert_allclose(T[:3,2],[0,0,-1]);np.testing.assert_allclose(T[:3,3],[0,0,-.0762])
 def test_clock_drift_is_mapped_and_boot_clears_history(self):
  clock=CaptureClock()
  for n in range(20):
   value=metadata(n,offset=100.+n*.0001);t,u,_=clock.observe(value)
  self.assertAlmostEqual(t,119.0019,places=4);self.assertLess(u,.003)
  clock.observe(metadata(0,boot="new"));self.assertEqual(len(clock.rows),1)
 def test_replayed_capture_rejected(self):
  clock=CaptureClock();clock.observe(metadata(1))
  with self.assertRaises(ValueError):clock.observe(metadata(1))
 def test_malformed_rotation_and_enabled_camera_without_url_rejected(self):
  value=copy.deepcopy(DEFAULT);value["rear"]["T_front_camera_rear_camera_m"][0][0]=2
  with self.assertRaises(ValueError):validate(value)
  value=copy.deepcopy(DEFAULT);value["rear"]["enabled"]=True
  with self.assertRaises(ValueError):validate(value)
 def pair(self,movement):
  import cv2
  rng=np.random.default_rng(42)
  xyz=np.column_stack([rng.uniform(-1,1,50),rng.uniform(-.6,.6,50),rng.uniform(2,4,50)])
  K=np.array([[300.,0,320],[0,300.,240],[0,0,1.]])
  des=rng.integers(0,256,size=(50,32),dtype=np.uint8);frames=[]
  for translation in ([0.,0.,0.],movement):
   pose=np.eye(4);pose[:3,3]=translation
   uv,_=cv2.projectPoints(xyz,np.zeros(3),-np.array(translation),K,np.zeros(5))
   frames.append({"pose":pose,"kps":uv.reshape(-1,2).astype(np.float32),"des":des.copy()})
  return xyz,frames
 def test_rear_temporal_triangulation_recovers_common_map_coordinates(self):
  xyz,frames=self.pair([.12,0,0])
  result=triangulate_pair(*frames,CAL)
  self.assertIsNotNone(result);points,i1,i2=result
  np.testing.assert_allclose(points,xyz[i1],atol=1e-4);np.testing.assert_array_equal(i1,i2)
 def test_stationary_pair_never_creates_fake_landmarks(self):
  _,frames=self.pair([0.,0.,0.]);self.assertIsNone(triangulate_pair(*frames,CAL))
 def test_rotation_interpolation(self):
  A=np.eye(4);B=np.asarray(DEFAULT_MOUNT,float);T=interpolate_pose(A,B,.5)
  np.testing.assert_allclose(T[:3,:3].T@T[:3,:3],np.eye(3),atol=1e-8)
  self.assertAlmostEqual(T[2,3],-.0381)
 def test_config_hot_reload_clears_temporal_pair_without_map_creation(self):
  with tempfile.TemporaryDirectory() as d:
   service=SimpleNamespace(state_dir=Path(d),map_id="ONE")
   rig=SharedCameraRig(service);rig.previous={"old":True}
   value=copy.deepcopy(DEFAULT);value["revision"]=2;value["rear"]["T_front_camera_rear_camera_m"][2][3]=-.09
   rig.configure(value);rig._reload()
   self.assertIsNone(rig.previous);self.assertEqual(service.map_id,"ONE")
   self.assertEqual(rig.config["revision"],2)
 def test_clock_uncertainty_prevents_pose_pairing(self):
  with tempfile.TemporaryDirectory() as d:
   backend=SimpleNamespace(rig_resolve=lambda a:np.eye(4))
   service=SimpleNamespace(state_dir=Path(d),map_id="ONE",backend=backend)
   rig=SharedCameraRig(service);rig.front.extend([(1.,.2,{},("a","b"),"ONE"),(1.2,.2,{},("a","b"),"ONE")])
   self.assertIsNone(rig._pose_at(1.1,.001))

if __name__=="__main__":unittest.main()
