"""Native pySLAM graph admission qualification; opt in with PYSLAM_ROOT."""
import os,sys,copy,tempfile,threading,unittest
from pathlib import Path
from types import SimpleNamespace
import numpy as np
ROOT=os.environ.get("PYSLAM_ROOT")
@unittest.skipUnless(ROOT,"set PYSLAM_ROOT for installed native dependency")
class NativeRigMapTest(unittest.TestCase):
 def test_rear_keyframes_and_points_use_one_native_map_and_checkpoint(self):
  os.environ["PYSLAM_USE_CPP"]="false";sys.path.insert(0,ROOT)
  from pyslam.config import Config
  Config()
  from pyslam.config_parameters import Parameters
  Parameters.USE_CPP_CORE=False
  from pyslam.local_features.feature_tracker_configs import FeatureTrackerConfigs
  from pyslam.local_features.feature_tracker import feature_tracker_factory
  from pyslam.slam.feature_tracker_shared import FeatureTrackerShared
  FeatureTrackerShared.set_feature_tracker(feature_tracker_factory(**copy.deepcopy(FeatureTrackerConfigs.ORB2)),force=True)
  from pyslam.slam import Map,KeyFrame
  from slam_backend import SlamBackend,next_native_frame_index
  from rig_native_map import camera,frame_with_features,commit_pair,metadata,write_state,restore
  from test_camera_rig import CAL,RigGeometryTests
  from camera_rig import DEFAULT,triangulate_pair
  xyz,frames=RigGeometryTests().pair([.12,0,0])
  for i,f in enumerate(frames):
   f.update(image=np.zeros((480,640,3),np.uint8),octaves=np.zeros(50,np.uint8),sizes=np.full(50,31.,np.float32),
     angles=np.zeros(50,np.float32),frame_id="rear-boot:"+str(i),time=1.+i*.2,map_id="SHARED",mount_revision=1,matches=[])
  with tempfile.TemporaryDirectory() as directory:
   backend=SlamBackend({},Path(directory))
   native=Map();queued=[]
   backend._slam=SimpleNamespace(map=native,local_mapping=SimpleNamespace(push_keyframe=lambda *args:queued.append(args[0])))
   backend._map_id="SHARED";backend._next_frame_index=1;backend._last_loop_id=0
   cam=camera(CAL);front=frame_with_features(cam,frames[0],0);front.camera_id="front"
   reference=KeyFrame(front,frames[0]["image"]);reference.camera_id="front"
   native.add_frame(reference);native.add_keyframe(reference);backend._source_frames={"0":"front-boot:1"}
   identity=id(native);temporal=list(native.frames)
   result=commit_pair(backend,frames[0],frames[1],triangulate_pair(*frames,CAL),CAL,DEFAULT)
   self.assertEqual(id(backend._slam.map),identity);self.assertEqual(result["map_id"],"SHARED")
   self.assertGreater(result["added_points"],10);self.assertEqual(native.num_keyframes(),3)
   self.assertEqual(list(native.frames),temporal);self.assertEqual(len(queued),2)
   self.assertTrue(all(p.map is native for p in native.get_points()))
   self.assertGreater(next_native_frame_index(backend._slam,backend._next_frame_index),max(k.img_id for k in native.get_keyframes()))
   before={point.id:point.pt().copy() for point in native.get_points()}
   shift=np.eye(4);shift[0,3]=2.;reference.update_pose(np.linalg.inv(shift@reference.Twc()))
   backend._read_entities()
   for point in native.get_points():
    np.testing.assert_allclose(point.pt(),before[point.id]+[2,0,0],atol=1e-6)
   target=Path(directory)/"map.json";native.save(str(target));write_state(directory,metadata(backend))
   restored=Map();restored.load(str(target));backend._slam.map=restored;restore(backend,directory)
   self.assertEqual(restored.num_keyframes(),3);self.assertGreater(restored.num_points(),10)
   self.assertEqual(sum(getattr(k,"camera_id","front")=="rear" for k in restored.get_keyframes()),2)
   self.assertEqual(len(backend._rig_anchors),2)
if __name__=="__main__":unittest.main()
