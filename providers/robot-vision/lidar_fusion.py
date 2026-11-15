"""Pushed VL53L5CX zone depths fused with the existing visual SLAM map.

Transforms map optical sensor coordinates (right, down, forward) to front camera.
Depth is axial/perpendicular, as reported by the VL53L5CX ULD, not radial range.
Mounts and zone orientation are configurable; initial mounts are provisional.
"""
import copy, hashlib, json, math, sqlite3, threading, time
from collections import deque
import numpy as np
from navigation_frames import rigid_transform
from vision_config import atomic_json

def defaults():
    front=np.eye(4);front[1,3]=-.02
    rear=np.diag([-1.,1.,-1.,1.]);rear[1,3]=-.02;rear[2,3]=-.0762
    return {"schema":"rover.sensor-rig/1","revision":1,"lidar":{
        name:{"camera_id":name,"T_front_camera_sensor_m":T.tolist(),
              "fov_horizontal_deg":45.,"fov_vertical_deg":45.,
              "flip_x":True,"flip_y":True,"valid_statuses":[5,9],
              "distance_semantics":"axial_z","module_size_m":[.025,.025,.005],
              "qualification":"provisional_20mm_above_corresponding_camera",
              "zone_orientation":"ST_lens_inverts_both_axes_provisional_mount_orientation"}
        for name,T in (("front",front),("rear",rear))}}

def validate_config(value):
    if not isinstance(value,dict) or value.get("schema")!="rover.sensor-rig/1":
        raise ValueError("sensor_rig_schema")
    if type(value.get("revision")) is not int or value["revision"]<1:
        raise ValueError("sensor_rig_revision")
    for name,row in value["lidar"].items():
        rigid_transform(row["T_front_camera_sensor_m"])
        for key in ("fov_horizontal_deg","fov_vertical_deg"):
            if type(row.get(key)) not in (int,float) or not 0<row[key]<180:raise ValueError(key)
        if row.get("distance_semantics") not in ("axial_z","radial"):raise ValueError("lidar_distance_semantics")
        if any(type(row.get(key)) is not bool for key in ("flip_x","flip_y")):raise ValueError("zone_orientation")
        if not row.get("valid_statuses") or any(type(v) is not int for v in row["valid_statuses"]):raise ValueError("lidar_target_status")
    return copy.deepcopy(value)

def zone_points(row,mount,capture_us):
    if not row.get("valid") or row.get("ready") is False:return []
    observed=row.get("observed_monotonic_us")
    if type(observed) is not int or observed<0 or observed>capture_us or capture_us-observed>1000000:return []
    n=row.get("resolution",64);side=math.isqrt(n)
    distances=row.get("distance_mm");statuses=row.get("target_status")
    if side*side!=n or side not in (4,8) or not isinstance(distances,list) or not isinstance(statuses,list) or len(distances)!=n or len(statuses)!=n:return []
    tx=math.tan(math.radians(mount["fov_horizontal_deg"])/2)
    ty=math.tan(math.radians(mount["fov_vertical_deg"])/2)
    out=[]
    for i,(distance,status) in enumerate(zip(distances,statuses)):
        if type(distance) not in (int,float) or not math.isfinite(distance) or distance<=0 or status not in mount["valid_statuses"]:continue
        x=(2*((i%side+.5)/side)-1)*tx;y=(2*((i//side+.5)/side)-1)*ty
        if mount["flip_x"]:x=-x
        if mount["flip_y"]:y=-y
        ray=np.array([x,y,1.]);z=distance/1000
        point=ray*z if mount["distance_semantics"]=="axial_z" else ray/np.linalg.norm(ray)*z
        out.append({"zone":i,"xyz_sensor_m":point.tolist(),"depth_m":float(point[2]),"target_status":status})
    return out

def scale_matches(native_camera_points,points,mount,side=8,scale=None):
    """Associate only interior-zone, frontmost visual landmarks with measured depth."""
    T=np.asarray(mount["T_front_camera_sensor_m"]);native=np.asarray(native_camera_points)
    if native.size==0:return []
    q=(native-T[:3,3]/scale)@T[:3,:3] if scale else native@T[:3,:3]
    tx=math.tan(math.radians(mount["fov_horizontal_deg"])/2);ty=math.tan(math.radians(mount["fov_vertical_deg"])/2)
    bins={}
    for index,p in enumerate(q):
        if not np.isfinite(p).all() or p[2]<=0:continue
        u=(p[0]/p[2]/tx+1)*side/2;v=(p[1]/p[2]/ty+1)*side/2
        if mount["flip_x"]:u=side-u
        if mount["flip_y"]:v=side-v
        if not 0<=u<side or not 0<=v<side:continue
        # Zone boundary associations are ambiguous.
        if min(u%1,1-u%1,v%1,1-v%1)<.1:continue
        zone=int(v)*side+int(u)
        if zone not in bins or p[2]<bins[zone][1]:bins[zone]=(index,p[2])
    shift=float((T[:3,:3].T@T[:3,3])[2])
    matches=[]
    for point in points:
        found=bins.get(point["zone"])
        if found:
            index,z=found
            native_z=z+(shift/scale if scale else 0.)
            if native_z<=0:continue
            factor=(point["depth_m"]+shift)/native_z
            if math.isfinite(factor) and factor>0:matches.append((index,factor,point["zone"]))
    return matches

class LidarFusion:
    def __init__(self,service):
        self.survey_landmarks = None
        if getattr(service,'atlas',None) is not None:
            from survey_lidar_landmarks import SurveyLidarLandmarks
            self.survey_landmarks=SurveyLidarLandmarks(service.state_dir,service.atlas)
            service.atlas.survey_lidar=self.survey_landmarks
        self.service=service;self.path=service.state_dir/"sensor-rig.json"
        self.config=validate_config(json.loads(self.path.read_text())) if self.path.exists() else defaults()
        self.stamp=None;self.lock=threading.RLock();self.event=threading.Event();self.stop=threading.Event()
        self.pending=None;self.latest=None;self.raw_latest=None;self.visual=deque(maxlen=128);self.scale=None;self.error=None
        self.scans = {name:deque(maxlen=64) for name in self.config["lidar"]}
        self.processed=0;self.dropped=0;self.last_key=None;self.thread=None
        self.db=sqlite3.connect(str(service.state_dir/"lidar-map.sqlite3"),check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS voxels(map_id TEXT,anchor_id TEXT,voxel TEXT,data TEXT,PRIMARY KEY(map_id,anchor_id,voxel))")
        self.db.commit()
    def start(self):
        self.thread=threading.Thread(target=self._run,name="slam-lidar-fusion",daemon=True);self.thread.start();return self
    def configure(self,value):
        config=validate_config(value)
        with self.lock:
            config["revision"]=max(self.config["revision"]+1,config["revision"])
            atomic_json(self.path,config);self.config=config;self.scale=None
        return self.status()
    def enqueue(self,packet,interval):
        if not packet.get("lidar"):return
        with self.lock:
            capture=packet.get("capture_monotonic_us")
            for raw in packet["lidar"].get("sensors",[]):
                name=raw.get("id")
                us=raw.get("observed_monotonic_us")
                if name not in self.config["lidar"] or not isinstance(us,int) or not isinstance(capture,int):
                    continue
                history=self.scans.setdefault(name,deque(maxlen=64))
                identity=(packet.get("receiver_id"),packet.get("boot_id"),us)
                if history and history[-1][0]==identity:continue
                if not (0<=capture-us<=1000000 and len(interval)==2):continue
                dt=(capture-us)/1e6
                history.append((identity,(interval[0]-dt,interval[1]-dt),raw,capture,self.service.map_id))
            if self.pending:self.dropped+=1
            self.raw_latest=(copy.deepcopy(packet),list(interval),time.monotonic())
            self.pending=(copy.deepcopy(packet),list(interval),self.service.map_id)
        self.event.set()
    def for_camera(self,frame,name,cal,camera_mount=None):
        from native_lidar_depth import project_depth
        packet=frame.get("sensor_packet") or {}
        boot=(packet.get("receiver_id"),packet.get("boot_id"))
        image_us=frame.get("capture_monotonic_us")
        if not isinstance(image_us,int):image_us=packet.get("capture_monotonic_us")
        interval=frame.get("capture_host_monotonic_interval_s")
        tick=getattr(getattr(self.service,"sensor_ticks",None),"_push_snapshot",None)
        if name=="front" and tick and isinstance(image_us,int) and boot==(tick[0].get("receiver_id"),tick[0].get("boot_id")):
            delta=(image_us-tick[0]["capture_monotonic_us"])/1e6
            interval=[x+delta for x in tick[1]]
        with self.lock:
            history=list(self.scans.get(name,()))
            mount=copy.deepcopy(self.config["lidar"].get(name))
            revision=self.config["revision"]
        if not mount:return None
        candidates=[]
        for identity,window,raw,capture,map_id in history:
            if map_id!=self.service.map_id:continue
            if identity[:2]==boot and isinstance(image_us,int):
                error=abs(image_us-identity[2])/1e6
            elif interval is not None and len(interval)==2:
                error=max(abs(interval[1]-window[0]),abs(window[1]-interval[0]))
            else:continue
            if error<=.18:candidates.append((error,identity,raw,capture))
        for error,identity,raw,capture in sorted(candidates,key=lambda c:c[0]):
            points=zone_points(raw,mount,capture)
            if not points:continue
            transform=np.asarray(mount["T_front_camera_sensor_m"])
            if camera_mount is not None:transform=np.linalg.solve(camera_mount,transform)
            depth,zones=project_depth(points,mount,math.isqrt(raw["resolution"]),cal,transform)
            return dict(depth=depth,zones=zones,scan_id=":".join(map(str,identity)),rig_revision=revision,
                        skew_bound_s=error,sensor_id=name,map_id=self.service.map_id)
        backend=getattr(self.service,"backend",None)
        if (name=="front" and self.survey_landmarks and getattr(backend,"_state",None)=="initializing"
                and not getattr(backend,"_native_birth",None) and not backend._geometry.get("keyframe_count")):
            seed=self.survey_landmarks.depth_at_verified_start(name,revision,interval)
            if seed:
                rows,reference=seed
                resolution=max(r["resolution"] for r in rows)
                distances=[0.]*resolution;statuses=[0]*resolution
                for r in rows:
                    if r["resolution"]==resolution:
                        distances[r["zone"]]=r["range_m"]*1000
                        statuses[r["zone"]]=mount["valid_statuses"][0]
                raw=dict(valid=True,resolution=resolution,distance_mm=distances,target_status=statuses,observed_monotonic_us=0)
                points=zone_points(raw,mount,0)
                depth,zones=project_depth(points,mount,math.isqrt(resolution),cal,mount["T_front_camera_sensor_m"])
                return dict(depth=depth,zones=zones,scan_id="survey:"+reference["zero_id"]+":"+name,
                            rig_revision=revision,skew_bound_s=None,sensor_id=name,map_id=self.service.map_id,
                            survey_reference=reference)
        return None

    def observe_visual(self,frame,localization):
        if not localization.get("valid"):return
        backend=self.service.backend
        if backend is None:return
        interval=frame.get("capture_host_monotonic_interval_s")
        ticks=getattr(getattr(self.service,"sensor_ticks",None),"_push_snapshot",None)
        packet=frame.get("sensor_packet") or {}
        if ticks and all(packet.get(k)==ticks[0].get(k) for k in ("receiver_id","boot_id")):
            delta=(packet["capture_monotonic_us"]-ticks[0]["capture_monotonic_us"])/1e6
            interval=[v+delta for v in ticks[1]]
        if not interval:return
        anchor=backend.rig_anchor(localization["T_map_camera"])
        if anchor is None:return
        with self.lock:self.visual.append((sum(interval)/2,anchor,self.service.map_id))
    def _pose(self,t,map_id):
        from camera_rig import interpolate_pose
        with self.lock:rows=list(self.visual)
        backend=self.service.backend
        if backend is None:return None
        for a,b in zip(rows,rows[1:]):
            if a[2]==b[2]==map_id and a[0]<=t<=b[0] and 0<b[0]-a[0]<=self.service.camera_rig.config["rear"].get("max_pose_gap_s",2.):
                A=backend.rig_resolve(a[1]);B=backend.rig_resolve(b[1])
                if A is not None and B is not None:return interpolate_pose(A,B,(t-a[0])/(b[0]-a[0]))
        return None
    def _run(self):
        while not self.stop.is_set():
            self.event.wait(.2);self.event.clear()
            with self.lock:item=self.pending;self.pending=None
            if not item:continue
            try:self.process(*item)
            except Exception as exc:self.error=type(exc).__name__+":"+str(exc)[:120]
    def process(self,packet,interval,map_id):
        stamp=self.path.stat().st_mtime_ns if self.path.exists() else None
        if stamp!=self.stamp:
            if stamp is not None:self.config=validate_config(json.loads(self.path.read_text()))
            self.stamp=stamp
        identity=(packet.get("receiver_id"),packet.get("boot_id"),packet.get("stream_sequence"))
        if identity==self.last_key:return
        self.last_key=identity
        capture=packet["capture_monotonic_us"];rows=[]
        for row in packet["lidar"].get("sensors",[]):
            mount=self.config["lidar"].get(row.get("id"))
            if not mount:continue
            points=zone_points(row,mount,capture)
            rows.append({"sensor_id":row["id"],"valid":bool(points),"points":points,"raw":copy.deepcopy(row),"mount":copy.deepcopy(mount)})
        live={"schema":"rover.lidar-fusion/1","received_monotonic":time.monotonic(),"capture_host_interval_s":interval,
              "receiver_id":packet["receiver_id"],"boot_id":packet["boot_id"],"map_id":map_id,
              "packet_id":packet["frame_id"],"sensors":rows,"rig_revision":self.config["revision"],"units":"m",
              "mounts_qualification":"provisional_until_owner_measurements","pose_available":False}
        if self.survey_landmarks is not None:
            self.survey_landmarks.observe(rows,packet,interval,self.config["revision"])
        pose=self._pose(sum(interval)/2,map_id)
        # LiDAR arrives before the NEXT processed visual pose. Wait on this
        # independent worker for a causal bracket, while the ingress caches
        # current raw grids immediately and supersedes pending work.
        gap=getattr(getattr(self.service,"camera_rig",None),"config",{}).get("rear",{}).get("max_pose_gap_s",2.)
        deadline=time.monotonic()+gap
        while pose is None and self.visual and not self.stop.is_set() and time.monotonic()<deadline:
            self.stop.wait(.02);pose=self._pose(sum(interval)/2,map_id)
        backend=self.service.backend
        if map_id!=self.service.map_id:return
        if pose is not None and backend is not None:
            live["pose_available"]=True
            with backend._lock:
                if backend._slam is None:return
                with backend._slam.map.update_lock:
                    native=[p for p in backend._slam.map.get_points() if not p.is_bad()]
                    xyz=np.array([p.pt() for p in native]).reshape(-1,3)
                    q=(xyz-pose[:3,3])@pose[:3,:3]
                    geometry_epoch=backend._last_loop_id
            matches=[]
            prior=(self.scale or {}).get("scale_m_per_map_unit") if (self.scale or {}).get("map_id")==map_id else None
            supported=0
            for row in rows:
                depths,hits=self.survey_landmarks.visual_depths(packet["frame_id"],row) if self.survey_landmarks else (row["points"],0)
                supported+=hits
                side=math.isqrt(row["raw"].get("resolution",64))
                if side in (4,8):matches.extend(scale_matches(q,depths,row["mount"],side,prior))
            live["survey_depth_zones_used"]=supported
            if len(matches)>=6:
                factors=np.array([m[1] for m in matches]);median=float(np.median(factors))
                inliers=np.abs(factors-median)<=.12*median
                if inliers.sum()>=6 and len({matches[i][2] for i in np.flatnonzero(inliers)})>=4:
                    scatter=float(np.median(np.abs(factors[inliers]-median))/median)
                    self.scale={"availability":"available","map_id":map_id,"scale_m_per_map_unit":median,
                                "source":"lidar_visual_landmark_depth_correspondence","relative_mad":scatter,
                                "inliers":int(inliers.sum()),"rig_revision":self.config["revision"],
                                "qualification":"estimated_with_provisional_sensor_mounts"}
            attachment=self.service.metric.attachment_status(map_id)
            factor=attachment.get("scale_m_per_map_unit") if attachment.get("availability")=="available" else None
            if factor is None and self.scale and self.scale["map_id"]==map_id:factor=self.scale["scale_m_per_map_unit"]
            if getattr(backend,"_native_birth",None):
                factor=1.
                self.scale={"map_id":map_id,"availability":"available","scale_m_per_map_unit":1.,
                            "source":"native_lidar_visual_initialization"}
            live["scale"]=copy.deepcopy(self.scale);live["mapping_available"]=bool(factor)
            if factor:
                anchor=backend.rig_anchor(pose)
                if anchor:
                    T=np.asarray(anchor["T_keyframe_camera"]);records=[]
                    for row in rows:
                        M=np.asarray(row["mount"]["T_front_camera_sensor_m"])
                        for point in row["points"]:
                            camera=M[:3,:3]@point["xyz_sensor_m"]+M[:3,3]
                            local=T[:3,:3]@(camera/factor)+T[:3,3]
                            records.append((str(anchor["keyframe_id"]),local,row["sensor_id"],point["zone"]))
                    with self.lock,self.db:
                        for key,point,sensor,zone in records:
                            voxel=json.dumps(np.floor(point*factor/.05).astype(int).tolist())
                            value={"xyz_keyframe":point.tolist(),"sensor_id":sensor,"zone":zone,
                                   "rig_revision":self.config["revision"],"recorded_revision":self.service.revision+1,"scale_m_per_map_unit":factor}
                            self.db.execute("INSERT OR IGNORE INTO voxels VALUES(?,?,?,?)",(map_id,key,voxel,json.dumps(value)))
                    # Real range residuals refine native visual landmarks, using
                    # the same pose/map and retained metric scale. This is not a second SLAM map.
                    updates=0
                    with self.service._pipeline_lock,backend._lock:
                        if backend is self.service.backend and backend._map_id==map_id and backend._last_loop_id==geometry_epoch:
                            with backend._slam.map.update_lock:
                                for index,observed,_zone in matches:
                                    if abs(observed/factor-1)>.15:continue
                                    point=native[index]
                                    if point.is_bad():continue
                                    target=pose[:3,3]+(point.pt()-pose[:3,3])*(observed/factor)
                                    point.update_position(.9*point.pt()+.1*target);updates+=1
                            if updates:
                                self.service._commit_changes(backend._diff(backend._read_entities()),"lidar_depth_constraints")
                    if records:
                        with self.service._pipeline_lock:
                            self.service._commit_changes([{"op":"upsert","kind":"lidar_state","entity_id":"ledger","data":{"source":"lidar","rig_revision":self.config["revision"],"zones":len(records)}}],"lidar_observations")
                    live["native_landmarks_refined"]=updates;live["mapped_zones"]=len(records)
        self.processed+=1;self.error=None
        with self.lock:self.latest=live
    def status(self):
        with self.lock:
            latest=copy.deepcopy(self.latest);configuration=copy.deepcopy(self.config);raw=copy.deepcopy(self.raw_latest)
        if raw:
            packet,interval,received=raw
            latest=latest or {}
            latest["fusion_packet_id"]=latest.get("packet_id")
            latest.update(packet_id=packet["frame_id"],received_monotonic=received,
                          capture_host_interval_s=interval,sensors=[
                {"sensor_id":row["id"],"raw":row,"mount":copy.deepcopy(configuration["lidar"].get(row["id"],{}))}
                for row in packet["lidar"].get("sensors",[])])
        return {"state":"streaming" if latest and time.monotonic()-latest["received_monotonic"]<2 else "awaiting_sensor_push",
                "latest":latest,"configuration":configuration,"scale":copy.deepcopy(self.scale),
                "processed_scans":self.processed,"superseded_scans":self.dropped,"error":self.error,
                "worker":"slam-lidar-fusion","same_native_map":True,
                "survey_landmarks":dict(self.survey_landmarks.status) if self.survey_landmarks else {}}
    def entities(self,map_id,revision=None):
        backend=self.service.backend
        if backend is None:return []
        with self.lock:rows=self.db.execute("SELECT anchor_id,voxel,data FROM voxels WHERE map_id=?",(map_id,)).fetchall()
        anchors={};out=[]
        for key,voxel,raw in rows:
            if key not in anchors:
                anchor=self.service._sonar_anchor(map_id,key,revision);anchors[key]=None if anchor is None else np.asarray(anchor["T_map_keyframe"])
            T=anchors[key]
            if T is None:continue
            data=json.loads(raw)
            if revision is not None and data.get("recorded_revision",0)>revision:continue
            xyz=T[:3,:3]@data["xyz_keyframe"]+T[:3,3]
            out.append({"kind":"sparse_point","entity_id":"lidar:"+key+":"+hashlib.sha256(voxel.encode()).hexdigest()[:16],
                        "data":{"xyz":xyz.tolist(),"rgb":[40,220,220],"source":"lidar","sensor_id":data["sensor_id"],
                                "zone":data["zone"],"units":"arbitrary","rig_revision":data["rig_revision"],
                                "mount_qualification":"provisional"}})
        return out
    def close(self):
        self.stop.set();self.event.set()
        if self.thread:self.thread.join()
        self.db.close()
