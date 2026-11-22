"""Canonical display derivations, calculated once at the provider for all subscribers."""
import copy
import json
import logging
import math
from pathlib import Path
import threading
import time
import numpy as np

def matrix(value, n):
    try:
        a = np.asarray(value, dtype=float)
        return a if a.shape == (n,n) and np.isfinite(a).all() else None
    except (TypeError, ValueError):
        return None

def xyz(p):
    if not isinstance(p, dict): return None
    try:
        a = np.array([p[k] for k in ("x_m","y_m","z_m")], dtype=float)
        return a if np.isfinite(a).all() else None
    except (KeyError, TypeError, ValueError): return None

def rotation(a):
    if not isinstance(a,dict) or a.get("valid") is not True: return None
    q = a.get("quaternion_wxyz")
    if isinstance(q,list) and len(q)==4 and all(isinstance(v,(int,float)) and math.isfinite(v) for v in q):
        q=np.array(q);n=np.linalg.norm(q)
        if n>1e-8:
            w,x,y,z=q/n
            return np.array([[1-2*(y*y+z*z),2*(x*y-w*z),2*(x*z+w*y)],
                 [2*(x*y+w*z),1-2*(x*x+z*z),2*(y*z-w*x)],
                 [2*(x*z-w*y),2*(y*z+w*x),1-2*(x*x+y*y)]])
    try: r,p,y=[math.radians(a[k]) for k in ("roll_deg","pitch_deg","yaw_deg")]
    except (KeyError,TypeError,ValueError): return None
    cr,sr,cp,sp,cy,sy=math.cos(r),math.sin(r),math.cos(p),math.sin(p),math.cos(y),math.sin(y)
    return np.array([[cy*cp,cy*sp*sr-sy*cr,cy*sp*cr+sy*sr],
                     [sy*cp,sy*sp*sr+cy*cr,sy*sp*cr-cy*sr],[-sp,cp*sr,cp*cr]])

def range_fit(anchors, ranges):
    rows=[]
    for a in anchors:
        r=next((r for r in ranges if str(r.get("target_bssid","")).lower()==a["id"] and
            isinstance(r.get("distance_m"),(int,float)) and r["distance_m"]>0 and
            isinstance(r.get("age_s"),(int,float)) and 0<=r["age_s"]<=15),None)
        if r: rows.append((np.array(a["point"],float),r["distance_m"]))
    if len(rows)<4:return None
    p0,d0=rows[0]
    A=np.array([2*(p-p0) for p,d in rows[1:]])
    b=np.array([p@p-p0@p0+d0*d0-d*d for p,d in rows[1:]])
    if np.linalg.matrix_rank(A)<3:return None
    p=np.linalg.lstsq(A,b,rcond=None)[0]
    def loss(p):return sum((np.linalg.norm(p-a)-d)**2 for a,d in rows)
    for _ in range(60):
        J=np.array([(p-a)/max(1e-6,np.linalg.norm(p-a)) for a,d in rows])
        e=np.array([np.linalg.norm(p-a)-d for a,d in rows])
        step=np.linalg.solve(J.T@J+np.eye(3)*.001,J.T@e)
        improved=False
        for scale in (1,.5,.25,.125,.0625,.03125,.015625,.0078125):
            q=p-scale*step
            if loss(q)<loss(p):p=q;improved=True;break
        if not improved or np.linalg.norm(step)<1e-6:break
    if not np.isfinite(p).all():return None
    return {"point":p.tolist(),"residual":math.sqrt(loss(p)/len(rows)),"count":len(rows),"approximate":True}

FLOORS={"14:63:93:91:86:e1":"downstairs","14:63:93:91:a4:31":"downstairs",
        "14:63:93:94:0f:29":"downstairs","14:63:93:91:86:89":"upstairs","14:63:93:94:0f:e1":"upstairs"}
def display_fit(tof):
    b=tof.get("baseline") or {}
    anchors=[];merged={}
    for p in [*(b.get("positions") or tof.get("anchor_geometry") or []),*(b.get("display_positions") or [])]:
        key=str(p.get("bssid","")).lower()
        if (merged.get(key) or {}).get("position_source")!="measured_fixed":merged[key]=p
    for p in merged.values():
        point=xyz(p);key=str(p.get("bssid","")).lower()
        if point is not None and key:anchors.append({"id":key,"point":point.tolist(),
            "floor":(p.get("floor_prior") or {}).get("floor",FLOORS.get(key))})
    fresh=isinstance(tof.get("collector_age_s"),(int,float)) and 0<=tof["collector_age_s"]<=5
    raw=tof.get("position") or {}
    matches=lambda p:p.get("frame_id", (tof.get("frame") or {}).get("frame_id"))==b.get("frame_id") and p.get("baseline_epoch")==b.get("epoch") and p.get("baseline_revision")==b.get("revision")
    position=xyz(raw) if fresh and tof.get("valid") and matches(raw) else None
    estimate=None;source="unavailable"
    refinement=tof.get("rover_refinement")
    saved=(refinement or {}).get("estimate") or {}
    if position is not None:source="position"
    elif fresh and (tof.get("registration") or {}).get("valid") is True and all((tof["registration"].get(k)==b.get(v)) for k,v in (("frame_id","frame_id"),("baseline_epoch","epoch"),("baseline_revision","revision"))) and isinstance((tof["registration"].get("current_camera") or {}).get("position_anchor_m"),list) and len(tof["registration"]["current_camera"]["position_anchor_m"])==3 and 0<=(tof["registration"]["current_camera"].get("age_s") if isinstance(tof["registration"]["current_camera"].get("age_s"),(int,float)) else 999)<=3:
        q=np.asarray(tof["registration"]["current_camera"]["position_anchor_m"],float)
        if np.isfinite(q).all():position=q;source="registered_visual"
    elif fresh and refinement and refinement.get("state")!="moving" and saved.get("frame_id")==b.get("frame_id") and saved.get("baseline_epoch")==b.get("epoch") and saved.get("baseline_revision")==b.get("revision") and isinstance(saved.get("residual_rms_m"),(int,float)) and math.isfinite(saved["residual_rms_m"]):
        position=xyz(saved)
        if position is not None:
            estimate={"point":position.tolist(),"residual":saved.get("residual_rms_m"),"refined":True,"sessions":refinement.get("session_count")}
            source="refinement"
    elif fresh and not refinement:
        estimate=range_fit(anchors,tof.get("rover_ranges") or [])
        if estimate:position=np.array(estimate["point"]);source="range_fit"
    floor=None
    if b.get("world_alignment")!="configured_east_north_up":
        down=np.array([a["point"] for a in anchors if a["floor"]=="downstairs"])
        up=np.array([a["point"] for a in anchors if a["floor"]=="upstairs"])
        if len(down)>=3 and len(up):
            origin=down.mean(axis=0);delta=up.mean(axis=0)-origin;distance=np.linalg.norm(delta)
            if distance>=.05:
                z=delta/distance;ref=np.array([1.,0,0] if abs(z[0])<.9 else [0.,1,0])
                x=ref-z*(ref@z);x/=np.linalg.norm(x);axes=np.array([x,np.cross(z,x),z])
                floor={"origin":origin.tolist(),"axes":axes.tolist(),"separation":float(distance)}
                for a in anchors:a["point"]=(axes@(np.array(a["point"])-origin)).tolist()
                if position is not None:position=axes@(position-origin)
    return {"schema":"rover.tof.display-fit/1","source":source,"frame_id":b.get("frame_id"),
        "fresh":fresh,"estimate":estimate,"point":position.tolist() if position is not None else None,
        "anchors":anchors,"floor":floor,"approximate":source not in ("position","registered_visual","unavailable")}

class StreamDerivations:
    def __init__(self, service):
        self.service=service;self.lock=threading.RLock();self.stamps=None
        self.reference_snapshot = ({}, {}, {})
        self.reference_stop = threading.Event()
        self.reference_thread = None
        self.tof_snapshot = None
        self.previous=None;self.hits={};self.world=None;self.latest_tof=None;self.tof_at=0;self.latest_attitude=None;self.accepted_at=-math.inf;self.zero_id=None;self.attitude_at=-math.inf;self.orientation_world=None

    def start(self):
        if self.reference_thread is None:
            self.reference_thread = threading.Thread(target=self._watch_references,
                name="rover-stream-references", daemon=True)
            self.reference_thread.start()

    def close(self):
        self.reference_stop.set()
        if self.reference_thread is not None:
            self.reference_thread.join()

    def _watch_references(self):
        last_report = -math.inf
        while not self.reference_stop.is_set():
            try:
                self.refresh_references()
                value = self.service._read_tof()
            except Exception as error:
                # An earlier reading retains its real age and still expires.
                now = time.monotonic()
                if now - last_report >= 30:
                    logging.getLogger(__name__).warning("Deferred stream reference refresh: %s",
                                                       type(error).__name__)
                    last_report = now
            else:
                self.tof_snapshot = (time.monotonic(), value)
            self.reference_stop.wait(.2)

    def capture_tof(self):
        snapshot = self.tof_snapshot
        if snapshot is None:
            return {"schema": "rover.tof.solution/1", "status": "collector_unavailable",
                    "valid": False, "position": None, "navigation_usable": False}
        started, supplied = snapshot
        value = copy.deepcopy(supplied)
        elapsed = time.monotonic() - started
        age = value.get("collector_age_s")
        if isinstance(age, (int, float)):
            value["collector_age_s"] = age + elapsed
            if value["collector_age_s"] > 5:
                value.update(valid=False, status="collector_stale", position=None, position_hypotheses=[])
        registration = value.get("registration") or {}
        camera = registration.get("current_camera") or {}
        if isinstance(camera.get("age_s"), (int, float)):
            camera["age_s"] += elapsed
        return value

    def refresh_references(self):
        """Replace survey and mounting references without blocking live readers."""
        root=Path(self.service.state_dir)
        paths=[root/"survey-world.json",root/"attitude-world-reference.json",root/"robot-model.json"]
        stamps=[]
        for path in paths:
            try:
                stat = path.stat()
            except FileNotFoundError:
                stamps.append(None)
            except OSError:
                return
            else:
                stamps.append((stat.st_ino, stat.st_mtime_ns, stat.st_size))
        if stamps!=self.stamps:
            values=[]
            for path in paths[:2]:
                try:value = json.loads(path.read_text())
                except (FileNotFoundError,ValueError):value = {}
                except OSError:return
                values.append(value if isinstance(value, dict) else {})
            definition = self.service._robot_model().get("definition", {})
            self.reference_snapshot = (*values, definition)
            self.stamps = stamps

    def attitude_observation(self,meta):
        now=time.monotonic();ahrs=meta.get("ahrs") or {};cache=ahrs.get("snapshot") or {}
        telemetry=cache.get("telemetry") or {};raw=ahrs.get("ahrs") or telemetry.get("ahrs") or {}
        row=(ahrs.get("sensors") or {}).get("attitude") or {}
        a=raw.get("attitude") or row.get("data") or row
        identity=(ahrs.get("receiver_id") or telemetry.get("receiver_id"),ahrs.get("boot_id") or telemetry.get("boot_id"))
        previous=self.latest_attitude
        if any(identity) and previous and identity!=previous[0]:
            self.latest_attitude=previous=None
        age=a.get("age_upper_bound_ms",a.get("age_ms",row.get("age_ms")))
        if isinstance(age,(int,float)):
            age+=max(0.,now-meta.get("processed_monotonic",now))*1000
            if "age_upper_bound_ms" not in a:age+=max(0.,cache.get("elapsed_since_receive_s") or 0)*1000
        device=a.get("observed_uptime_ms",row.get("observed_uptime_ms"))
        imu=raw.get("imu") or {}
        if device is None and all(isinstance(v,(int,float)) for v in (imu.get("observed_uptime_ms"),imu.get("age_ms"),a.get("age_ms"))):
            device=imu["observed_uptime_ms"]+imu["age_ms"]-a["age_ms"]
        complete=a.get("valid") is True and raw.get("paused") is not True and raw.get("worker_ready") is not False and rotation(a) is not None
        newer=previous is None or (device>previous[1] if isinstance(device,(int,float)) and isinstance(previous[1],(int,float)) else isinstance(age,(int,float)) and now-age/1000>=previous[2])
        reference_changed=previous is not None and (a.get("world_reference_id"),a.get("generation"))!=(previous[3].get("world_reference_id"),previous[3].get("generation"))
        if complete and isinstance(age,(int,float)) and math.isfinite(age) and age>=0 and (newer or reference_changed):
            self.latest_attitude=(identity,device,now-age/1000,copy.deepcopy(a),copy.deepcopy(raw.get("calibration") or ahrs.get("calibration") or {}))
        held=self.latest_attitude
        if held is None:return {},None,ahrs.get("calibration") or {},identity
        return held[3],max(0.,now-held[2])*1000,held[4],held[0]

    def orientation(self,meta,baseline,reference):
        now=time.monotonic();tof=meta.get("tof") or {};ahrs=meta.get("ahrs") or {}
        world=baseline.get("world_id");ref=baseline.get("reference") or {}
        if world!=self.orientation_world:
            self.previous=None;self.accepted_at=-math.inf;self.zero_id=None;self.attitude_at=-math.inf;self.orientation_world=world
        T=matrix(ref.get("T_enu_body"),4)
        if T is None:return {"schema":"rover.world.orientation/1","source":"fallback","age_s":None,"valid":False,"pose":None,"world_id":world}
        T=T.copy();source="fallback";detail="surveyed checkpoint";age=None
        live=meta.get("world_body_pose") or tof.get("world_body_pose") or {}
        accepted=(tof.get("inertial_navigation") or {}).get("world_body_pose") or (live if live.get("schema")=="rover.world-body-pose/1" else {})
        if accepted.get("schema")=="rover.world-body-pose/1" and accepted.get("world_id")==world:
            point=accepted.get("position_enu_m");at=accepted.get("observed_at")
            collector=tof.get("collector_age_s")
            if isinstance(point,list) and len(point)==3 and all(isinstance(v,(int,float)) and math.isfinite(v) for v in point) and isinstance(at,(int,float)) and at>=self.accepted_at and isinstance(collector,(int,float)) and 0<=collector<=5:
                base=self.previous[1].copy() if self.previous and self.previous[0]==world else T.copy()
                base[:3,3]=point
                rotation_accepted=matrix(accepted.get("rotation_enu_body"),3)
                attitude_at=accepted.get("attitude_observed_at")
                if accepted.get("survey_zero_id") and rotation_accepted is not None and isinstance(attitude_at,(int,float)) and attitude_at>=self.attitude_at:
                    base[:3,:3]=rotation_accepted;self.zero_id=accepted["survey_zero_id"];self.attitude_at=attitude_at
                self.accepted_at=at
                live={"valid":True,"world_id":world,"T_enu_body":base.tolist(),"age_s":collector,
                      "source":accepted.get("source","accepted inertial body pose")}
        age=live.get("age_s")
        elapsed=max(0.,now-meta.get("processed_monotonic",now))
        if isinstance(age,(int,float)):age+=elapsed
        candidate=matrix(live.get("T_enu_body"),4)
        if candidate is not None and live.get("valid") is True and live.get("world_id",world)==world and isinstance(age,(int,float)) and 0<=age<=3:
            T=candidate.copy();source="world_body_pose";detail=live.get("source","world body pose")
        else:
            if self.previous and self.previous[0]==world:T=self.previous[1].copy();detail="last known pose"
            a,sensor_age,calibration,identity=self.attitude_observation(meta)
            # The display retains the newest valid device-clock sample through gaps.
            # A later camera packet cannot roll it back or renew its observation age.
            fresh=a.get("valid") is True and isinstance(sensor_age,(int,float))
            imu=matrix(ref.get("T_enu_imu",ref.get("T_enu_body")),4)
            body=matrix(ref.get("T_enu_body"),4)
            if fresh and imu is not None and not self.zero_id:
                persistent=matrix(a.get("world_rotation_enu_imu"),3)
                if a.get("world_reference_id") and a.get("world_alignment_valid") is True and a.get("world_id")==world and persistent is not None and np.allclose(persistent@persistent.T,np.eye(3),atol=.003) and abs(np.linalg.det(persistent)-1)<.003:
                    T[:3,:3]=persistent@imu[:3,:3].T@body[:3,:3];detail="persistent AHRS"
                elif not calibration.get("world_reference_persistent"):
                    initial=(baseline.get("checkpoint") or {}).get("attitude") or {}
                    saved=reference.get("attitude") or {}
                    matches=(reference.get("schema")=="rover.attitude.world-reference/1" and reference.get("world_id")==world and
                        reference.get("boot_id")==identity[1] and saved.get("generation")==a.get("generation") and
                        (not reference.get("receiver_id") or reference["receiver_id"]==identity[0]) and
                        (not isinstance(saved.get("yaw_origin_uptime_ms"),(int,float)) or saved["yaw_origin_uptime_ms"]==a.get("yaw_origin_uptime_ms")))
                    if matches:
                        initial=saved
                        selected=reference.get("reference") or {}
                        ri=matrix(selected.get("T_enu_imu"),4);rb=matrix(selected.get("T_enu_body"),4)
                        if ri is not None and rb is not None:imu,body=ri,rb
                    R0,R1=rotation(initial),rotation(a)
                    if R0 is None:
                        gravity=(ref.get("imu_checkpoint") or {}).get("observed_accel_imu_m_s2") or ((baseline.get("checkpoint") or {}).get("imu") or {}).get("accel_m_s2")
                        if isinstance(gravity,list) and len(gravity)==3:
                            x,y,z=gravity
                            R0=rotation({"valid":True,"roll_deg":math.degrees(math.atan2(y,z)),
                                "pitch_deg":math.degrees(math.atan2(-x,math.hypot(y,z))),"yaw_deg":0})
                    if R0 is not None and R1 is not None:
                        T[:3,:3]=imu[:3,:3]@R0.T@R1@imu[:3,:3].T@body[:3,:3];detail="survey-aligned AHRS" if matches else "baseline AHRS; heading unqualified"
                age=sensor_age/1000
            nav=tof.get("inertial_navigation") or {};b=tof.get("baseline") or {}
            p=xyz(nav.get("position"))
            if (p is not None and nav.get("world_id")==world and nav.get("frame_id")==b.get("frame_id")
                and nav.get("baseline_epoch")==b.get("epoch") and nav.get("state") in ("stationary","propagating")
                and 0<=tof.get("collector_age_s",999)<=5):
                camera=matrix(ref.get("T_enu_camera"),4)
                offset=body[:3,:3].T@(camera[:3,3]-body[:3,3]) if camera is not None else np.zeros(3)
                T[:3,3]=p-T[:3,:3]@offset
        if source=="fallback":
            b=tof.get("baseline") or {};nav=tof.get("inertial_navigation") or {}
            fresh=0<=tof.get("collector_age_s",999)<=5
            if fresh and b.get("world_alignment")=="configured_east_north_up" and not nav.get("suppress_range_fallback"):
                nav_ok=nav.get("world_id")==world and nav.get("state") in ("stationary","propagating") and xyz(nav.get("position")) is not None
                matches=lambda p:p.get("frame_id")==b.get("frame_id") and p.get("baseline_epoch")==b.get("epoch") and p.get("baseline_revision")==b.get("revision")
                position=None
                if not nav_ok:
                    for candidate in [tof.get("position") or {},((tof.get("rover_refinement") or {}).get("estimate") or {}) if (tof.get("rover_refinement") or {}).get("state")!="moving" else {}]:
                        if matches(candidate) and xyz(candidate) is not None:
                            position=xyz(candidate);break
                    reg=tof.get("registration") or {};cam=reg.get("current_camera") or {}
                    if position is None and reg.get("valid") is True and reg.get("frame_id")==b.get("frame_id") and reg.get("baseline_epoch")==b.get("epoch") and reg.get("baseline_revision")==b.get("revision") and 0<=cam.get("age_s",999)<=3:
                        q=cam.get("position_anchor_m")
                        if isinstance(q,list) and len(q)==3 and all(isinstance(x,(int,float)) and math.isfinite(x) for x in q):position=np.array(q)
                    if position is None:
                        excluded=set((tof.get("rover_refinement") or {}).get("range_exclusions") or [])
                        anchors={}
                        for a in [*(b.get("display_positions") or []),*(b.get("positions") or [])]:
                            key=str(a.get("bssid","")).lower();point=xyz(a)
                            if point is not None and key not in excluded:anchors[key]={"id":key,"point":point.tolist()}
                        fit=range_fit(list(anchors.values()),[v for v in tof.get("rover_ranges") or [] if v.get("target_bssid") not in excluded])
                        if fit:position=np.array(fit["point"]);detail+="; best-fit ranging estimate (unqualified)"
                    if position is not None:
                        body=matrix(ref.get("T_enu_body"),4);camera=matrix(ref.get("T_enu_camera"),4)
                        offset=body[:3,:3].T@(camera[:3,3]-body[:3,3]) if camera is not None else np.zeros(3)
                        T[:3,3]=position-T[:3,:3]@offset
        self.previous=(world,T.copy())
        # This is a coordinate conversion for the renderer, not a second estimate.
        swap=np.array([[1,0,0],[0,0,1],[0,1,0]])
        body_axes=np.array([[0,0,1],[-1,0,0],[0,1,0]])
        R=swap@T[:3,:3]@body_axes
        pose={"x":float(T[0,3]),"y":float(T[2,3]),"z":float(T[1,3]),"yaw":math.atan2(R[0,2],R[2,2]),
              "rotation":R.tolist(),"speed":0,"steering":0,"source":detail,"orientationSource":detail,
              "positionHeld":source=="fallback"}
        return {"schema":"rover.world.orientation/1","source":source,"age_s":age,"valid":True,
                "world_id":world,"T_enu_body":T.tolist(),"pose":pose}

    def project(self,meta,orientation,definition):
        from lidar_fusion import zone_points
        lidar=meta.setdefault("lidar",{})
        if not isinstance(lidar,dict):meta["lidar"]=lidar={}
        latest=lidar.get("latest")
        if isinstance(latest,dict):
            latest.setdefault("mapping_available",False);latest.setdefault("native_landmarks_refined",0)
        if not orientation.get("valid"):
            lidar["world_hits"]=[];return
        T=matrix(orientation["T_enu_body"],4)
        front=matrix((definition.get("camera") or {}).get("T_robot_camera"),4)
        if front is None:lidar["world_hits"]=[];return
        world=orientation.get("world_id")
        if world!=self.world:self.hits={};self.world=world
        now=time.monotonic()
        for row in (latest or {}).get("sensors",[]):
            raw=row.get("raw") or row;sid=row.get("sensor_id",row.get("id"))
            mount=row.get("mount") or (lidar.get("configuration") or {}).get("lidar",{}).get(sid,{})
            M=matrix(mount.get("T_front_camera_sensor_m"),4)
            if M is None:continue
            if raw.get("valid") is not True or raw.get("ready") is False:
                self.hits.pop(sid,None);continue
            observed=raw.get("observed_monotonic_us")
            if type(observed) is not int:continue
            received=(latest or {}).get("received_monotonic",now)
            age=max(0,now-received)+max(0,raw.get("age_ms") or 0)/1000
            key=((latest or {}).get("boot_id"),observed,raw.get("sequence"))
            old=self.hits.get(sid)
            if old and old[0]==key:continue
            if age>2:continue
            points=zone_points(raw,mount,observed)
            if not points:
                self.hits.pop(sid,None);continue
            transform=T@front@M;hits=[];stamp=time.time()-age
            for p in points:
                position=transform[:3,:3]@p["xyz_sensor_m"]+transform[:3,3]
                hits.append({"sensor_id":sid,"zone":p["zone"],"xyz_m":position.tolist(),
                             "status":p["target_status"],"t":stamp})
            self.hits[sid]=(key,hits)
        lidar["world_hits"]=[hit for _,hits in self.hits.values() for hit in hits]

    def enrich(self,metadata,image):
        with self.lock:
            now=time.monotonic()
            if isinstance(metadata.get("tof"),dict) and metadata["tof"].get("schema")=="rover.tof.solution/1":
                self.latest_tof=copy.deepcopy(metadata["tof"]);self.tof_at=now
            elif self.latest_tof is not None:
                metadata["tof"]=copy.deepcopy(self.latest_tof)
                if isinstance(metadata["tof"].get("collector_age_s"),(int,float)):
                    metadata["tof"]["collector_age_s"]+=now-self.tof_at
            if not isinstance(metadata.get("tof"),dict):metadata["tof"]={}
            baseline,reference,definition=self.reference_snapshot
            orientation=self.orientation(metadata,baseline,reference)
            metadata["world_orientation"]=orientation
            tof=metadata.setdefault("tof",{})
            if isinstance(tof,dict):tof["display_fit"]=display_fit(tof)
            self.project(metadata,orientation,definition)
