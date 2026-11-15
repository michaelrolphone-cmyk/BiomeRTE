"""One rig, one native SLAM map; a separate rear feature/tracking worker.

Rear observations never instantiate SlamBackend, VisionStore, a second Map or atlas.
Only the existing backend's map writer admits native keyframes and landmarks.
"""
from __future__ import annotations
import base64, copy, json, math, threading, time
from collections import deque
from pathlib import Path
import numpy as np
from vision_config import atomic_json
from navigation_frames import rigid_transform
DEFAULT_MOUNT=[[-1.,0.,0.,0.],[0.,1.,0.,0.],[0.,0.,-1.,-.0762],[0.,0.,0.,1.]]
DEFAULT={"schema":"rover.camera-rig/1","revision":1,"rear":{"enabled":False,
 "camera":{"source_id":"rear-camera","url":"","synchronized_capture":True,"expected_fps":5.,
 "network_timeout_s":2.,"reconnect_delay_s":1.},
 "calibration":None,"T_front_camera_rear_camera_m":DEFAULT_MOUNT,
 "mount_qualification":"owner_provisional_same_height_opposite_heading_three_inches_behind",
 "max_clock_uncertainty_s":.05,"max_pose_gap_s":.6,"num_features":1600}}
def validate(value):
 if not isinstance(value,dict) or value.get("schema")!="rover.camera-rig/1":raise ValueError("rig_schema")
 rear=value["rear"];rigid_transform(rear["T_front_camera_rear_camera_m"])
 if type(rear.get("enabled")) is not bool:raise ValueError("rear_enabled_boolean")
 if rear["enabled"] and not rear.get("camera",{}).get("url"):raise ValueError("rear_camera_url_required")
 for key in ("max_clock_uncertainty_s","max_pose_gap_s"):
  if not math.isfinite(float(rear.get(key,DEFAULT["rear"][key]))) or float(rear.get(key,DEFAULT["rear"][key]))<=0:raise ValueError(key)
 return copy.deepcopy(value)

def sensor_clock_bounds(frame, sensor_interval):
 """Combine independent bounds on the same physical exposure."""
 if frame.get("capture_transport")!="websocket_push":
  return dict(frame,capture_host_monotonic_interval_s=sensor_interval)
 camera_interval=frame["capture_host_monotonic_interval_s"]
 low=max(camera_interval[0],sensor_interval[0]);high=min(camera_interval[1],sensor_interval[1])
 return dict(frame,capture_host_monotonic_interval_s=[low,high]) if low<=high else frame

class CaptureClock:
 """Boot-scoped device->host clock fit from capture HTTP timing intervals."""
 def __init__(self):self.boot=None;self.rows=deque(maxlen=120)
 def observe(self,meta):
  packet=meta.get("sensor_packet") or {}
  identity=(packet.get("receiver_id"),packet.get("boot_id"))
  interval=meta.get("capture_host_monotonic_interval_s");device=meta.get("capture_monotonic_us")
  if not identity[0] or not identity[1] or type(device) is not int or not isinstance(interval,(list,tuple)) or len(interval)!=2:raise ValueError("capture_clock_unavailable")
  lo,hi=map(float,interval)
  if not all(math.isfinite(x) for x in (lo,hi)) or hi<lo:raise ValueError("capture_clock_interval")
  if identity!=self.boot:self.boot=identity;self.rows.clear()
  x=device/1e6
  if self.rows and x<=self.rows[-1][0]:raise ValueError("replayed_capture")
  self.rows.append((x,(lo+hi)/2,(hi-lo)/2))
  if meta.get("capture_transport")=="websocket_push":
   # The socket receiver already aligned this boot's device clock. Refitting
   # midpoints would add network jitter back to the exposure interval.
   return (lo+hi)/2,(hi-lo)/2,identity
  # Least-jitter samples constrain offset; drift is fitted only over a useful span.
  rows=sorted(self.rows,key=lambda r:r[2])[:max(2,len(self.rows)//2)]
  slope=1.
  if len(rows)>=4 and max(r[0] for r in rows)-min(r[0] for r in rows)>5:
   xx=np.array([r[0] for r in rows]);yy=np.array([r[1] for r in rows])
   centered=xx-xx.mean();slope=float(centered@(yy-yy.mean())/(centered@centered))
   if not .995<=slope<=1.005:raise ValueError("clock_drift_unqualified")
  offsets=[r[1]-slope*r[0] for r in rows];offset=float(np.median(offsets))
  uncertainty=max(min(r[2] for r in rows),max(abs(r[1]-(slope*r[0]+offset)) for r in rows))
  return slope*x+offset,uncertainty,identity

def interpolate_pose(a,b,fraction):
 import cv2
 A=np.asarray(a,dtype=float);B=np.asarray(b,dtype=float);out=np.eye(4)
 rod,_=cv2.Rodrigues(A[:3,:3].T@B[:3,:3])
 delta,_=cv2.Rodrigues(rod*float(fraction))
 out[:3,:3]=A[:3,:3]@delta;out[:3,3]=(1-fraction)*A[:3,3]+fraction*B[:3,3]
 return out

def triangulate_pair(previous,current,calibration,ratio=.75):
 import cv2
 if previous["des"] is None or current["des"] is None:return None
 matches=cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(previous["des"],current["des"],k=2)
 pairs=[m for row in matches if len(row)==2 for m,n in [row] if m.distance<ratio*n.distance]
 # Each detection can support at most one landmark observation.
 unique={};pairs=sorted(pairs,key=lambda m:m.distance)
 for m in pairs:unique.setdefault(m.trainIdx,m)
 pairs=list(unique.values())
 if len(pairs)<12:return None
 idx1=np.array([m.queryIdx for m in pairs]);idx2=np.array([m.trainIdx for m in pairs])
 K=np.array([[calibration["fx"],0,calibration["cx"]],[0,calibration["fy"],calibration["cy"]],[0,0,1.]])
 D=np.asarray(calibration["distortion"])
 uv1=previous["kps"][idx1];uv2=current["kps"][idx2]
 p1=cv2.undistortPoints(uv1.reshape(-1,1,2),K,D).reshape(-1,2)
 p2=cv2.undistortPoints(uv2.reshape(-1,1,2),K,D).reshape(-1,2)
 T1=np.linalg.inv(previous["pose"]);T2=np.linalg.inv(current["pose"])
 homogeneous=cv2.triangulatePoints(T1[:3],T2[:3],p1.T,p2.T)
 with np.errstate(divide="ignore",invalid="ignore"):points=(homogeneous[:3]/homogeneous[3]).T
 z1=(points@T1[:3,:3].T+T1[:3,3])[:,2];z2=(points@T2[:3,:3].T+T2[:3,3])[:,2]
 ray1=points-previous["pose"][:3,3];ray2=points-current["pose"][:3,3]
 with np.errstate(divide="ignore",invalid="ignore"):
  cosine=np.sum(ray1*ray2,axis=1)/(np.linalg.norm(ray1,axis=1)*np.linalg.norm(ray2,axis=1))
 reprojections=[]
 for T,uv in ((T1,uv1),(T2,uv2)):
  r,_=cv2.Rodrigues(T[:3,:3]);projected,_=cv2.projectPoints(points,r,T[:3,3],K,D)
  reprojections.append(np.linalg.norm(projected.reshape(-1,2)-uv,axis=1))
 valid=np.isfinite(points).all(axis=1)&(z1>0)&(z2>0)&(cosine<math.cos(math.radians(1)))&(cosine>0)
 valid&=(reprojections[0]<3)&(reprojections[1]<3)
 if valid.sum()<12:return None
 return points[valid],idx1[valid],idx2[valid]

class SharedCameraRig:
 def __init__(self,service):
  self.service=service;self.path=service.state_dir/"camera-rig.json"
  self.lock=threading.RLock();self.stop=threading.Event();self.thread=None
  self.publisher=None;self.camera=None;self.latest=None;self.error=None;self.processed=0;self.skipped=0
  self.front_clock=CaptureClock();self.rear_clock=CaptureClock();self.front=deque(maxlen=128)
  self.pending_rear=deque(maxlen=self.front.maxlen)
  self.pending_epoch=None;self.last_front_check=None;self.delayed_pairs=0
  self.config=validate(json.loads(self.path.read_text())) if self.path.exists() else copy.deepcopy(DEFAULT)
  self.stamp=None;self.previous=None;self.source_epoch=None;self.snapshot=None
  from rear_sensor_push import RearSensorIngress
  self.sensors=RearSensorIngress(self)
 def configure(self,value):
  if "sensor_rig" in value:
   self.service.lidar.configure(value["sensor_rig"])
  value=validate(value);atomic_json(self.path,value)
  return {"availability":"available","configuration":value,"applied":"worker_hot_reload"}
 def start(self):
  self.sensors.start()
  if self.thread is None:
   self.thread=threading.Thread(target=self._run,name="slam-rear-tracking",daemon=True);self.thread.start()
  return self
 def _raw_metadata(self,captured,previous):
  packet=captured.metadata.get("sensor_packet") or {}
  same=previous.get("frame_id")==captured.metadata["frame_id"] and previous.get("map_id")==self.service.map_id
  pose=copy.deepcopy(previous.get("localization")) if same else None
  pose=pose or {"valid":False,"reason":self.error or "rear_processing_pending"}
  return dict(captured.metadata,camera_id="rear",source_id="rear-camera",
   map_id=self.service.map_id,map_revision=self.service.revision,session_id=self.service.session_id,
   source_timestamp=captured.metadata.get("source_timestamp"),processing_timestamp=previous.get("processing_timestamp") if same else None,
   calibration_revision=(self.config["rear"].get("calibration") or {}).get("calibration_id"),
   image_available=True,camera_delivery="independent_rear_capture",
   processed_monotonic=captured.decoded_monotonic,processed_monotonic_basis="camera_receive_decode_time",
   processing_state="complete" if same else "pending",pose=pose,
   analysis={key:previous.get(key) for key in ("frame_id","map_id","map_revision","localization","processed_monotonic_s")},
   sensor_packet=packet,lidar=packet.get("lidar"),lidar_fusion=self.service.lidar.status() if getattr(self.service,"lidar",None) else None,ftm=packet.get("ftm"),uart=packet.get("uart"),
   rig_revision=self.config.get("revision"),T_front_camera_rear_camera_m=self.config["rear"]["T_front_camera_rear_camera_m"])
 def _clock_metadata(self,metadata):
  interval=self.sensors.capture_interval(metadata.get("sensor_packet") or {},time.monotonic())
  if interval is None:return metadata
  return sensor_clock_bounds(metadata,interval)

 def observe_front(self,frame,localization):
  if not localization.get("valid"):return
  tick=getattr(getattr(self.service,"sensor_ticks",None),"_push_snapshot",None)
  if tick:
   packet,interval=tick
   source=frame.get("sensor_packet") or {}
   if all(source.get(key)==packet.get(key) for key in ("receiver_id","boot_id")) and type(source.get("capture_monotonic_us")) is int:
    delta=(source["capture_monotonic_us"]-packet["capture_monotonic_us"])/1e6
    drift=abs(delta)*.0001
    frame=sensor_clock_bounds(frame,[interval[0]+delta-drift,interval[1]+delta+drift])
  try:t,u,boot=self.front_clock.observe(frame)
  except ValueError:return
  backend=self.service.backend
  if backend is None or not hasattr(backend,"rig_anchor"):return
  anchor=backend.rig_anchor(localization["T_map_camera"])
  if anchor is None:return
  with self.lock:
   if self.front and (self.front[-1][3]!=boot or self.front[-1][4]!=self.service.map_id):self.front.clear()
   self.front.append((t,u,anchor,boot,self.service.map_id))
 def _pose_at(self,t,u):
  cfg=self.config["rear"];backend=self.service.backend
  if backend is None:return None
  with self.lock:rows=list(self.front)
  for a,b in zip(rows,rows[1:]):
   if a[0]<=t<=b[0] and b[0]-a[0]<=cfg.get("max_pose_gap_s",.6):
    uncertainty=max(u,a[1],b[1])
    if uncertainty>cfg.get("max_clock_uncertainty_s",.05):return None
    if a[4]!=self.service.map_id or b[4]!=self.service.map_id:return None
    A=backend.rig_resolve(a[2]);B=backend.rig_resolve(b[2])
    if A is None or B is None:return None
    pose=interpolate_pose(A,B,(t-a[0])/(b[0]-a[0]))
    return pose,uncertainty
  return None
 def _select_tracking_frame(self,captured,t,u,boot):
  from dataclasses import replace
  import cv2
  epoch=(self.service.map_id,boot,self.config.get("revision"))
  if epoch!=self.pending_epoch:
   self.pending_rear.clear();self.last_front_check=None;self.pending_epoch=epoch
  pair=self._pose_at(t,u)
  if pair:
   self.pending_rear.clear()
   return captured,t,u,pair
  # The GUI continues to publish the newest image. Keep compressed exposures
  # only for matching to valid front poses that finish tracking later.
  if captured.jpeg_bytes:
   self.pending_rear.append((t,u,replace(captured,image=None)))
  with self.lock:
   watermark=self.front[-1][0] if self.front else None
  if watermark is not None and watermark!=self.last_front_check:
   self.last_front_check=watermark
   previous=self.previous or {}
   for when,uncertainty,old in reversed(self.pending_rear):
    if when>watermark:continue
    if previous.get("map_id")==epoch[0] and when<=previous["time"]:break
    paired=self._pose_at(when,uncertainty)
    if paired:
     image=cv2.imdecode(np.frombuffer(old.jpeg_bytes,dtype=np.uint8),cv2.IMREAD_COLOR)
     if image is not None:
      while self.pending_rear and self.pending_rear[0][0]<=when:self.pending_rear.popleft()
      self.delayed_pairs+=1
      return replace(old,image=image),when,uncertainty,paired
  return captured,t,u,None
 def _reload(self):
  stamp=self.path.stat().st_mtime_ns if self.path.exists() else None
  if stamp==self.stamp:return
  value=validate(json.loads(self.path.read_text())) if stamp else copy.deepcopy(DEFAULT)
  if self.camera:self.camera.close();self.camera=None
  if self.publisher:self.publisher.close();self.publisher=None
  self.config=value;self.stamp=stamp;self.previous=None;self.front.clear();self.rear_clock=CaptureClock()
  rear=value["rear"]
  if rear["enabled"]:
   from camera_source import CameraSource
   self.camera=CameraSource(rear["camera"]).start()
   from live_camera import LiveCamera
   self.publisher=LiveCamera(self.camera,self.service.rear_live_frames,self._raw_metadata).start()
   self.publisher._thread.name="rear-live-camera"
 def _run(self):
  import cv2
  orb=None;sequence=0;feature_count=None;camera_source=None
  while not self.stop.is_set():
   try:
    self._reload()
    if not self.camera:self.stop.wait(.2);continue
    if self.camera is not camera_source:sequence=0;camera_source=self.camera
    captured=self.camera.next_frame(sequence,timeout=.2)
    if captured is None:continue
    self.skipped+=max(0,captured.sequence-sequence-1);sequence=captured.sequence
    with self.lock:
     self.latest=captured;self.snapshot=None
    cfg=self.config["rear"];cal=cfg.get("calibration")
    from slam_backend import _calibration_error
    if _calibration_error(cal):self.error="rear_intrinsics_unqualified";continue
    if feature_count!=cfg.get("num_features",1600):
     feature_count=cfg.get("num_features",1600);orb=cv2.ORB_create(nfeatures=feature_count)
    if captured.image.shape[:2]!=(cal["height"],cal["width"]):raise ValueError("rear_calibration_size_mismatch")
    t,u,boot=self.rear_clock.observe(self._clock_metadata(captured.metadata))
    if boot!=self.source_epoch:self.previous=None;self.source_epoch=boot
    captured,t,u,paired=self._select_tracking_frame(captured,t,u,boot)
    from image_preprocessing import preprocess_slam_image
    backend=self.service.backend
    policy=getattr(backend,"_active_preprocessing",{"mode":"none","version":1})
    tracking_image,preprocessing=preprocess_slam_image(captured.image,policy)
    keypoints,des=orb.detectAndCompute(tracking_image,None)
    if des is None or len(keypoints)<12:self.error="rear_features_insufficient";continue
    kps=np.float32([k.pt for k in keypoints])
    current={"image":tracking_image,"preprocessing":preprocessing,"kps":kps,"des":des,"octaves":np.uint8([k.octave for k in keypoints]),
     "sizes":np.float32([k.size for k in keypoints]),"angles":np.float32([k.angle for k in keypoints]),
     "frame_id":captured.metadata["frame_id"],"time":t,"clock_uncertainty_s":u,
     "mount_revision":self.config.get("revision",1),"map_id":self.service.map_id,"matches":[]}
    backend=self.service.backend
    if backend is None:self.error="shared_backend_unavailable";continue
    # Match only landmarks belonging to the ONE existing native map.
    from rig_native_map import match_shared_map
    current["geometry_epoch"]=backend._last_loop_id
    estimate,matches=match_shared_map(backend,current,cal)
    if paired:
     scale=self.service.metric.attachment_status(self.service.map_id)
     factor=scale.get("scale_m_per_map_unit") if scale.get("availability")=="available" else None
     if not factor and getattr(self.service,"lidar",None) is not None:
      lidar_scale=self.service.lidar.scale
      if lidar_scale and lidar_scale.get("map_id")==self.service.map_id:factor=lidar_scale.get("scale_m_per_map_unit")
     if not factor or not math.isfinite(factor) or factor<=0:
      self.error="metric_scale_required_for_rig_baseline";continue
     mount=np.asarray(cfg["T_front_camera_rear_camera_m"],float).copy();mount[:3,3]/=factor
     seed=paired[0]@mount
     # Rigid synchronization is authoritative for common-map bootstrap.
     # Independent rear PnP is retained as a consistency observation.
     current.update(pose=seed,rig_pose=seed,scale_m_per_map_unit=factor)
     if estimate is not None:
      delta=np.linalg.inv(seed)@estimate
      angle=math.acos(float(np.clip((np.trace(delta[:3,:3])-1)/2,-1,1)))
      if np.linalg.norm(delta[:3,3])*factor>.15 or angle>math.radians(15):
       self.error="rear_pose_disagrees_with_synchronized_rig";continue
    elif estimate is not None:
     current.update(pose=estimate,rig_pose=None,scale_m_per_map_unit=None)
    else:self.error="awaiting_synchronized_front_pose_or_shared_map_relocalization";continue
    current["matches"]=matches
    if self.previous and self.previous.get("geometry_epoch")!=current["geometry_epoch"]:self.previous=None
    if self.previous and self.previous.get("native_keyframe_id") and backend._slam is not None:
     for keyframe in backend._slam.map.get_keyframes():
      if str(keyframe.id)==self.previous["native_keyframe_id"] and not keyframe.is_bad():self.previous["pose"]=keyframe.Twc();break
    pair=triangulate_pair(self.previous,current,cal) if self.previous and self.previous["map_id"]==current["map_id"] else None
    result={"added_points":0}
    if pair is not None:
     with self.service._pipeline_lock:
      if self.service.backend is not backend or self.service.map_id!=current["map_id"]:self.previous=None;continue
      from rig_native_map import commit_pair
      result=commit_pair(backend,self.previous,current,pair,cal,self.config)
      self.service._commit_changes(result["map_changes"],"rig_rear_mapping")
    with self.lock:self.snapshot={"availability":"available","camera_id":"rear","map_id":self.service.map_id,
     "map_revision":self.service.revision,"frame_id":current["frame_id"],"localization":{
      "valid":True,"T_map_camera":current["pose"].tolist(),"units":"arbitrary","scale_status":"shared_front_map"},
     "clock_uncertainty_s":u,"rig_revision":current["mount_revision"],"added_points":result["added_points"],
     "processed_monotonic_s":time.monotonic(),"capture_host_monotonic_s":t}
    if self.publisher:self.publisher.record_processed(captured,self.snapshot)
    self.previous=current;self.processed+=1;self.error=None
   except Exception as exc:
    self.previous=None
    self.error=type(exc).__name__+":"+str(exc)[:240];self.stop.wait(.1)
 def status(self):
  return {"map_id":self.service.map_id,"map_ownership":"existing_front_backend_same_native_Map",
   "enabled":self.config["rear"]["enabled"],"worker_thread":self.thread.name if self.thread else None,
   "alive":bool(self.thread and self.thread.is_alive()),"processed_frames":self.processed,
   "skipped_frames":self.skipped,"delayed_pose_pairs":self.delayed_pairs,"front_pose_samples":len(self.front),"error":self.error,"camera":self.camera.status() if self.camera else None,
   "sensor_rig":self.service.lidar.config if getattr(self.service,"lidar",None) else None,
   "sensor_push":self.sensors.status(),"live_camera":self.publisher.status() if self.publisher else None,
   "configuration":copy.deepcopy(self.config),"clock_sync":"boot_scoped_device_capture_to_shared_host_monotonic"}
 def query(self,operation,inputs):
  with self.lock:captured=self.latest;snapshot=copy.deepcopy(self.snapshot)
  if operation=="robot.vision.frame":
   if captured is None:return {"availability":"unavailable","camera_id":"rear","reason":"no_rear_frame"}
   jpeg=captured.jpeg_bytes
   if jpeg is None:
    import cv2
    ok,encoded=cv2.imencode(".jpg",captured.image)
    if not ok:raise ValueError("rear_jpeg_encode_failed")
    jpeg=encoded.tobytes()
   return {"availability":"available","camera_id":"rear","map_id":self.service.map_id,
    "frame":{"frame_id":captured.metadata["frame_id"],"metadata":captured.metadata,
     "mime_type":"image/jpeg","encoding":"base64","data":base64.b64encode(jpeg).decode()}}
  if snapshot and time.monotonic()-snapshot.get("capture_host_monotonic_s",snapshot.get("processed_monotonic_s",0))>self.service.config.get("freshness_threshold_s",2.):
   snapshot["availability"]="unavailable";snapshot["localization"]["valid"]=False;snapshot["reason"]="rear_pose_stale"
  return snapshot or {"availability":"unavailable","camera_id":"rear","map_id":self.service.map_id,
                      "reason":self.error or "rear_tracking_not_ready"}
 def close(self):
  self.sensors.close()
  self.stop.set()
  if self.camera:self.camera.close()
  if self.publisher:self.publisher.close()
  if self.thread:self.thread.join()
