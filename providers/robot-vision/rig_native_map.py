"""Admission of rear observations into the existing native Map.

Feature extraction, matching, PnP and triangulation happen on the rear worker.
This module serializes only native graph admission with existing backend writes.
It never allocates a second Slam or Map and never modifies front temporal frames.
"""
import copy,json,math
from pathlib import Path
from types import SimpleNamespace
import numpy as np

def anchor(backend,pose):
 with backend._lock:
  runtime=backend._slam
  if runtime is None:return None
  refs=[kf for kf in runtime.map.get_keyframes() if not kf.is_bad() and getattr(kf,"camera_id","front")=="front"]
  if not refs:return None
  reference=max(refs,key=lambda k:k.kid)
  return {"map_id":backend._map_id,"keyframe_id":str(reference.id),
          "T_keyframe_camera":(np.linalg.inv(reference.Twc())@np.asarray(pose)).tolist()}
def resolve(backend,value):
 with backend._lock:
  if backend._slam is None or value.get("map_id")!=backend._map_id:return None
  for kf in backend._slam.map.get_keyframes():
   if str(kf.id)==value["keyframe_id"] and not kf.is_bad():
    return kf.Twc()@np.asarray(value["T_keyframe_camera"])
 return None

def match_shared_map(backend,current,calibration):
 import cv2
 with backend._lock:
  runtime=backend._slam
  if runtime is None or backend._map_id!=current["map_id"]:return None,[]
  points=list(runtime.map.get_points())
 usable=[p for p in points if not p.is_bad() and getattr(p,"des",None) is not None]
 if len(usable)<12:return None,[]
 descriptors=np.ascontiguousarray([p.des.copy() for p in usable],dtype=np.uint8)
 xyz=np.asarray([p.pt() for p in usable],float)
 pairs=cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(descriptors,current["des"],k=2)
 matches={}
 for i,row in enumerate(pairs):
  if len(row)==2 and row[0].distance<.75*row[1].distance:
   m=row[0]
   if m.trainIdx not in matches or m.distance<matches[m.trainIdx][0]:
    matches[m.trainIdx]=(m.distance,i)
 if len(matches)<12:return None,[]
 indices=list(matches);pidx=[matches[k][1] for k in indices]
 K=np.array([[calibration["fx"],0,calibration["cx"]],[0,calibration["fy"],calibration["cy"]],[0,0,1.]])
 ok,r,t,inliers=cv2.solvePnPRansac(xyz[pidx],current["kps"][indices],K,
  np.asarray(calibration["distortion"]),iterationsCount=100,reprojectionError=3.,confidence=.999)
 if not ok or inliers is None or len(inliers)<12:return None,[]
 R,_=cv2.Rodrigues(r);T=np.eye(4);T[:3,:3]=R;T[:3,3]=t.ravel()
 good=inliers.ravel()
 return np.linalg.inv(T),[(usable[pidx[i]],indices[i]) for i in good]

def camera(calibration):
 from pyslam.slam import PinholeCamera
 from pyslam.io.dataset_types import SensorType
 settings={"Camera."+k:calibration[k] for k in ("width","height","fx","fy","cx","cy")}
 settings["Camera.fps"]=5.
 return PinholeCamera(SimpleNamespace(cam_settings=settings,DistCoef=calibration["distortion"],sensor_type=SensorType.MONOCULAR))

def frame_with_features(cam,data,index):
 from pyslam.slam import Frame
 frame=Frame(cam,None,pose=np.linalg.inv(data["pose"]),timestamp=data["time"],img_id=index)
 for name in ("kps","des","octaves","sizes","angles"):setattr(frame,name,np.ascontiguousarray(data[name]).copy())
 frame.kpsu=cam.undistort_points(frame.kps);frame.kpsn=cam.unproject_points(frame.kpsu)
 frame.points=np.full(len(frame.kps),None,dtype=object);frame.outliers=np.zeros(len(frame.kps),dtype=bool)
 frame.img=data["image"];frame.camera_id="rear"
 return frame

def _keyframe(backend,cam,data):
 from pyslam.slam import KeyFrame
 runtime=backend._slam
 existing=data.get("native_keyframe_id")
 if existing is not None:
  for kf in runtime.map.get_keyframes():
   if str(kf.id)==existing and not kf.is_bad():return kf
  raise ValueError("rear_reference_keyframe_retired")
 index=backend._next_frame_index;backend._next_frame_index+=1
 frame=frame_with_features(cam,data,index);kf=KeyFrame(frame,data["image"]);kf.camera_id="rear"
 runtime.map.add_keyframe(kf) # Deliberately do not append to front map.frames.
 backend._source_frames[str(index)]=data["frame_id"]
 backend._rig_sources[str(index)]="rear"
 data["native_keyframe_id"]=str(kf.id)
 for point,idx in data.get("matches",[]):
  if not point.is_bad() and point.map is runtime.map:point.add_observation(kf,int(idx))
 return kf

def commit_pair(backend,previous,current,triangulation,calibration,rig):
 with backend._lock:
  runtime=backend._slam
  if runtime is None or backend._map_id!=current["map_id"] or previous["map_id"]!=backend._map_id:
   raise ValueError("rig_map_changed")
  if previous["mount_revision"]!=current["mount_revision"]:raise ValueError("rig_mount_changed")
  if previous.get("geometry_epoch",backend._last_loop_id)!=backend._last_loop_id or current.get("geometry_epoch",backend._last_loop_id)!=backend._last_loop_id:raise ValueError("rig_geometry_changed")
  if not hasattr(backend,"_rig_sources"):backend._rig_sources={}
  if not hasattr(backend,"_rig_anchors"):backend._rig_anchors={}
  if not hasattr(backend,"_rig_calibrations"):backend._rig_calibrations={}
  cam=camera(calibration);pts,i1,i2=triangulation
  with runtime.map.update_lock:
   k1=_keyframe(backend,cam,previous);k2=_keyframe(backend,cam,current)
   # Keep both camera views in a single spanning tree and descriptor database.
   front=[k for k in runtime.map.get_keyframes() if not k.is_bad() and getattr(k,"camera_id","front")=="front"]
   if not front:raise ValueError("rig_requires_initialized_shared_map")
   ref=max(front,key=lambda k:k.kid)
   if k1.parent is None:k1.set_parent(ref)
   if k2.parent is None:k2.set_parent(k1)
   free=np.array([k1.points[int(a)] is None and k2.points[int(b)] is None for a,b in zip(i1,i2)])
   count,_,added=runtime.map.add_points(pts[free],None,k1,k2,i1[free],i2[free],previous["image"],do_check=True)
   for k in (k1,k2):
    k.update_connections()
    backend._rig_anchors[str(k.id)]={"reference_keyframe_id":str(ref.id),
     "T_reference_camera":(np.linalg.inv(ref.Twc())@k.Twc()).tolist(),
     "camera_id":"rear","rig_revision":rig.get("revision",1)}
   backend._rig_calibrations["rear"]=copy.deepcopy(calibration)
   # Native local mapping and loop detection consume these SAME keyframes.
   for k,data in ((k1,previous),(k2,current)):
    if not data.get("native_queued"):
     runtime.local_mapping.push_keyframe(k,data["image"],None,None)
     data["native_queued"]=True
  runtime.rig_next_frame_index=backend._next_frame_index
  entities=backend._read_entities()
  changes=backend._diff(entities,complete=False)
  return {"added_points":int(count),"map_changes":changes,"map_id":backend._map_id,
          "native_map_identity":id(runtime.map)}

def metadata(backend):
 return {"schema":"rover.shared-map-rig/1","sources":copy.deepcopy(getattr(backend,"_rig_sources",{})),
  "anchors":copy.deepcopy(getattr(backend,"_rig_anchors",{})),
  "calibrations":copy.deepcopy(getattr(backend,"_rig_calibrations",{}))}
def write_state(target,state):
 (Path(target)/"camera_rig.json").write_text(json.dumps(state,allow_nan=False,sort_keys=True))
def restore(backend,target):
 path=Path(target)/"camera_rig.json"
 state=json.loads(path.read_text()) if path.exists() else {}
 backend._rig_sources=state.get("sources",{});backend._rig_anchors=state.get("anchors",{})
 backend._rig_calibrations=state.get("calibrations",{})
 if not backend._rig_sources:return
 backend._slam.rig_next_frame_index=max(int(index) for index in backend._rig_sources)+1
 for kf in backend._slam.map.get_keyframes():
  kf.camera_id=backend._rig_sources.get(str(kf.img_id),"front")

def refresh_anchors(backend):
 """Carry rear rig observations through corrections of their front graph anchors."""
 runtime=backend._slam
 records=getattr(backend,"_rig_anchors",{})
 if runtime is None or not records:return
 with runtime.map.update_lock:
  frames={str(k.id):k for k in runtime.map.get_keyframes() if not k.is_bad()}
  deltas={}
  for identifier,record in records.items():
   child=frames.get(identifier);parent=frames.get(record["reference_keyframe_id"])
   if child is None or parent is None:continue
   old=child.Twc();desired=parent.Twc()@np.asarray(record["T_reference_camera"])
   if not np.allclose(old,desired,atol=1e-8):
    deltas[identifier]=desired@np.linalg.inv(old)
    child.update_pose(np.linalg.inv(desired))
  # Only move landmarks whose native reference is a corrected rear keyframe.
  # A landmark shared with the front retains its original native owner.
  for point in runtime.map.get_points():
   reference=point.get_reference_keyframe()
   delta=deltas.get(str(reference.id)) if reference is not None else None
   if delta is not None and not point.is_bad():
    position=point.pt();point.update_position(delta[:3,:3]@position+delta[:3,3])
