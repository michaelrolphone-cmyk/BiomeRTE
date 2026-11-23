"""Registry-driven instrument descriptions and generic state-file sample publishers."""
import copy
import json
import threading
import time
from pathlib import Path
import numpy as np

def transform(rotation, position):
    T=np.eye(4);T[:3,:3]=rotation;T[:3,3]=position;return T.tolist()

def configured_sensors(service):
    rig=service.lidar
    with rig.config_lock:
        stamp=(rig.path.stat().st_mtime_ns,rig.path.stat().st_size) if rig.path.exists() else None
        if stamp!=getattr(rig,"_manifest_rig_stamp",None) and stamp is not None:
            from lidar_fusion import validate_config
            updated=validate_config(json.loads(rig.path.read_text()))
            with rig.lock:rig.config=updated
        rig._manifest_rig_stamp=stamp
        with rig.lock:config=copy.deepcopy(rig.config)
        if True:  # Native entries are projections of the current rig, never stale copies.
            model=service._robot_model().get("definition",{})
            front=np.array(model.get("camera",{}).get("T_robot_camera",np.eye(4)))
            rear=service.camera_rig.config["rear"];rows=[]
            def add(id,kind,role,T=None,rate=None,path=None,selector=None):
                rows.append({"id":id,"type":kind,"role":role,"rate_hz":rate,"T_body_sensor":T,
                    "channel":"/vision","message_type":"frame" if kind=="camera" else "sensor_tick",
                    "schema":"rover.vision.frame/1" if kind=="camera" else "rover.sensor.tick/1",
                    "selector":selector or {"camera_id":role} if kind=="camera" else selector or {"sensor_id":role},
                    "path":path or ("payload" if kind=="camera" else "sensor_packet")})
            add("camera.front","camera","front",front.tolist(),service.config["camera"].get("expected_fps",5))
            if rear["enabled"]:
                add("camera.rear","camera","rear",(front@np.array(rear["T_front_camera_rear_camera_m"])).tolist(),rear["camera"].get("expected_fps",5))
            for name,mount in config.get("lidar",{}).items():
                add("lidar."+name,"lidar",name,(front@np.array(mount["T_front_camera_sensor_m"])).tolist(),
                    mount.get("rate_hz"),"lidar.latest.sensors")
            for name,mount in model.get("sonar_poses",{}).items():
                x,y,z=mount["direction_body"];R=[[x,-y,0],[y,x,0],[0,0,1]]
                add("ultrasonic."+name,"ultrasonic",name,transform(R,mount["position_body_m"]),None,
                    "sensor_packet.ultrasonic.ultrasonic.sensors",{"id":name,"path":"sensor_packet.ultrasonic.ultrasonic.sensors"})
            imu=model.get("ahrs_mounting",{})
            mount=transform(imu["R_body_from_imu"],imu["position_body_m"]) if imu.get("R_body_from_imu") and imu.get("position_body_m") else None
            for kind in ("imu","ahrs","mag","barometer"):
                add(kind+".front",kind,"front",mount,None,"ahrs.sensors."+("attitude" if kind=="ahrs" else kind),{"path":"ahrs.sensors."+("attitude" if kind=="ahrs" else kind)})
            add("tof.front","tof","front",front.tolist(),None,"tof",{"path":"tof"})
            for row in rows:row["native"]=True
            custom=[row for row in config.get("sensors",[]) if not row.get("native")]
            by_id={row["id"]:row for row in rows}
            for row in custom:
                if row.get("enabled") is False:by_id.pop(row["id"],None)
                else:by_id[row["id"]]=row
            rows=list(by_id.values())
            if config.get("sensors")!=rows:
                config["sensors"]=rows
                from vision_config import atomic_json
                atomic_json(rig.path,config)
                with rig.lock:rig.config=config
        return copy.deepcopy(config["sensors"])

def manifest(service, observations):
    from datetime import datetime,timezone
    import hashlib
    now=time.monotonic();epoch=time.time()-now;rows=[]
    for configured in configured_sensors(service):
        row=copy.deepcopy(configured);sample=observations.get(row["id"]);when=sample[1] if sample else None
        row.setdefault("schema","rover.sensor.tick/1");row.setdefault("rate_hz",None);row.setdefault("T_body_sensor",None)
        row.setdefault("channel","/vision");row.setdefault("message_type","sensor_tick");row.setdefault("selector",{"path":"sensor_packet.sensors","id":row["id"]})
        row["mount"]={"T_body_sensor":row["T_body_sensor"]}
        row["stream"]={"capability":"robot.vision.stream:1","channel":row["channel"].strip("/"),"path":row.pop("path","sensor_packet.sensors")}
        row.pop("source",None);row.pop("native",None)
        row["last_seen"]=datetime.fromtimestamp(epoch+when,timezone.utc).isoformat(timespec="milliseconds") if when is not None else None
        row["status"]=sample[2]["status"] if sample else "offline"
        if row["rate_hz"] is None and sample:row["rate_hz"]=sample[2].get("rate_hz")
        if when is None or now-when>max(2,3/(row["rate_hz"] or 1)):row["status"]="offline"
        rows.append(row)
    identity=[{k:v for k,v in row.items() if k!="last_seen"} for row in rows]
    revision=hashlib.sha256(json.dumps(identity,sort_keys=True,allow_nan=False).encode()).hexdigest()
    return {"type":"sensor_manifest","schema":"rover.sensor.manifest/1","revision":revision,"sensors":rows}

class RigSampleFeed:
    """Generic configured sources have independent clocks and never impersonate a camera."""
    def __init__(self,service):
        self.service=service;self.stop=threading.Event();self.thread=None;self.sent={};self.sequence=0
        import uuid
        self.boot=uuid.uuid4().hex
    def start(self):
        self.thread=threading.Thread(target=self.run,name="rig-sample-feed",daemon=True);self.thread.start()
    def collect(self,now=None):
        now=time.monotonic() if now is None else now;out=[]
        for row in configured_sensors(self.service):
            source=row.get("source")
            if not isinstance(source,dict):continue
            rate=row.get("rate_hz") or 1
            if rate<=0 or now-self.sent.get(row["id"],-float("inf"))<1/rate:continue
            self.sent[row["id"]]=now
            try:
                if source.get("kind")=="constant":value=copy.deepcopy(source["value"])
                elif source.get("kind")=="state_file":
                    path=(Path(self.service.state_dir)/source["path"]).resolve()
                    if not path.is_relative_to(Path(self.service.state_dir).resolve()):raise ValueError("rig_source_outside_state")
                    value=json.loads(path.read_text())
                else:continue
                out.append({"id":row["id"],"type":row["type"],"role":row["role"],"value":value,
                            "rate_hz":rate,"T_body_sensor":row.get("T_body_sensor"),"t":time.time()})
            except (OSError,ValueError,KeyError):continue
        return out
    def run(self):
        while not self.stop.wait(.02):
            rows=self.collect()
            if not rows:continue
            self.service.live_frames.publish_manifest(self.service.inventory.snapshot(self.service))
            self.sequence+=1;now=time.monotonic()
            self.service.live_frames.publish({"type":"sensor_tick","sensor_tick":True,"image_available":False,
                "frame_id":"rig:"+str(self.sequence),"camera_id":"rig","processed_monotonic":now,
                "sensor_packet":{"schema":"rover.sensor.samples/1","schema_version":{"major":1,"minor":0},"receiver_id":"rig-registry","boot_id":self.boot,
                    "clock":"host_monotonic","capture_monotonic_us":int(now*1000000),"stream_sequence":self.sequence,"sensors":rows}},b"")
    def close(self):
        self.stop.set()
        if self.thread:self.thread.join()
