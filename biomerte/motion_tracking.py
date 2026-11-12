"""Passive stream observer, durable asynchronous recorder and prediction API."""
import asyncio
import collections
import copy
import json
import logging
import math
import pathlib
import queue
import sqlite3
import threading
import time
from .motion_model import MotionModel, controls, number, wrap

def objects(value):
    if isinstance(value,dict):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value,list):
        for child in value:
            yield from objects(child)

def attitude(meta):
    evidence=meta.get("spatial_evidence",{})
    for obj in objects(evidence.get("ahrs",{})):
        candidate=obj.get("attitude")
        if (isinstance(candidate,dict) and number(candidate.get("yaw_deg"))
                and candidate.get("valid") is True and number(candidate.get("age_ms"))
                and 0<=candidate["age_ms"]<=500):
            return candidate["yaw_deg"]
    return None

def ranges(meta):
    result={}
    evidence=meta.get("spatial_evidence",{})
    for obj in objects(evidence.get("ultrasonic",meta.get("ultrasonic",{}))):
        sensors=obj.get("sensors")
        if isinstance(sensors,list):
            for sensor in sensors:
                if (isinstance(sensor,dict) and sensor.get("valid") is True
                        and sensor.get("status")=="ok" and number(sensor.get("distance_m"))
                        and number(sensor.get("age_ms")) and 0<=sensor["age_ms"]<=250):
                    result[sensor.get("id")]=dict(distance=sensor["distance_m"],
                        sequence=sensor.get("sequence"),age_ms=sensor["age_ms"])
    return result

def metric_delta(before,after):
    # Opposed front/rear ranges must agree on translation, with effectively
    # unchanged heading. A changing target/beam footprint is rejected.
    dy=wrap(after["yaw"]-before["yaw"])
    a,b=before["ranges"],after["ranges"]
    if abs(dy)>2. or not all(k in a and k in b for k in ("front","rear")):
        return None,"metric_geometry_unqualified"
    for k in ("front","rear"):
        if a[k]["sequence"]==b[k]["sequence"]:
            return None,"sonar_sample_reused"
    front=a["front"]["distance"]-b["front"]["distance"]
    rear=b["rear"]["distance"]-a["rear"]["distance"]
    if abs(front-rear)>.02+.2*max(abs(front),abs(rear)):
        return None,"opposed_sonar_disagreement"
    return (front+rear)/2.,"opposed_sonar_translation_hypothesis"

class MotionRecorder:
    def __init__(self,path):
        self.path=pathlib.Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.events=queue.SimpleQueue()
        self.error=None
        self.pending=0
        self.snapshot=None
        self.latest_model=None
        self.thread=threading.Thread(target=self.run,name="rover-motion-recorder",daemon=True)
        self.thread.start()

    def offer(self,kind,value,received=None):
        if self.error:
            return
        self.pending+=1
        self.events.put((kind,copy.deepcopy(value),received if received is not None else time.monotonic()))

    def run(self):
        try:
            db=sqlite3.connect(str(self.path))
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, kind TEXT, received_monotonic REAL, recorded_utc REAL, payload TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS model (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT, updated_utc REAL)")
            row=db.execute("SELECT payload FROM model WHERE id=1").fetchone()
            model=MotionModel(json.loads(row[0]) if row else None)
            previous=None
            sonar_history=[]
            timeline=[]
            identity=None
            last_sequence=None
            last_ack=None
            last_transitions=None
            while True:
                event=self.events.get()
                if event is None:
                    break
                kind,value,received=event
                self.pending-=1
                db.execute("INSERT INTO events(kind,received_monotonic,recorded_utc,payload) VALUES (?,?,?,?)",
                           (kind,received,time.time(),json.dumps(value,separators=(",",":"),allow_nan=False)))
                if kind=="gap":
                    previous=None
                    timeline=[]
                    last_ack=None
                    model.state["steering_known"]=False
                    model.state["speed_m_s"]=None
                    model.state["yaw_rate_deg_s"]=None
                elif kind=="control" and value.get("stage")=="applied":
                    current=(value.get("authority"),value.get("epoch"),value.get("generation"))
                    seq=value.get("sequence")
                    mask=value.get("commanded_mask")
                    try:controls(mask)
                    except ValueError:
                        model.reject("invalid_applied_mask")
                        continue
                    if current!=identity:
                        identity=current
                        previous=None
                        timeline=[]
                        last_sequence=None
                        last_transitions=None
                        model.state["steering_known"]=False
                    if type(seq) is not int or last_sequence is not None and seq<=last_sequence:
                        model.reject("duplicate_or_reordered_ack")
                    else:
                        last_sequence=seq
                        transitions=value.get("output_transitions")
                        if type(transitions) is int and last_transitions is not None and transitions>last_transitions+1:
                            model.reject("missed_output_transitions")
                            previous=None
                            timeline=[]
                            model.state["steering_known"]=False
                        if type(transitions) is int:last_transitions=transitions
                        last_ack=received
                        if not timeline or timeline[-1]["mask"]!=mask:
                            # The actual application is bracketed between issue
                            # and host receipt; never pretend ACK receipt is GPIO time.
                            issued=value.get("issued_utc_ms")
                            age=value.get("host_issue_age_s")
                            lower=received-max(0.,age) if number(age) else None
                            timeline.append(dict(mask=mask,lower=lower,upper=received,
                                provider_monotonic_ms=value.get("provider_monotonic_ms"),
                                identity=current,sequence=seq))
                elif kind=="sonar":
                    sonar_history.append(value)
                    if len(sonar_history)>2:
                        newest=value.get("source_received_at")
                        # Retain the latest few seconds in RAM; durable raw records remain complete.
                        sonar_history=sonar_history[-32:]
                elif kind=="vision":
                    frame=value.get("frame",{})
                    ahrs=value.get("spatial_evidence",{}).get("ahrs",{})
                    generation=ahrs.get("attitude_reference",{}).get("generation")
                    interval=frame.get("capture_host_monotonic_interval_s")
                    yaw=attitude(value)
                    if not isinstance(interval,list) or len(interval)!=2 or yaw is None:
                        model.reject("missing_capture_clock_or_ahrs")
                    else:
                        try:lo,hi=map(float,interval)
                        except (TypeError,ValueError):lo=hi=float("nan")
                        if not all(number(v) for v in (lo,hi)) or hi<lo or received-hi<0 or received-lo>2.:
                            model.reject("stale_or_invalid_capture")
                            previous=None
                        else:
                            sample=dict(lo=lo,hi=hi,t=(lo+hi)/2.,yaw=yaw,
                                frame_id=frame.get("frame_id",value.get("frame_id")),
                                boot=frame.get("capture_boot_id"),generation=generation,ranges=ranges(value))
                            capture=frame.get("capture_monotonic_us")
                            if number(capture):
                                for sonar in sonar_history:
                                    if sonar.get("boot_id")!=sample["boot"]:continue
                                    period=sonar.get("update_rate",{}).get("nominal_per_sensor_period_s") or .26
                                    for sensor in sonar.get("sensors",[]):
                                        at=sensor.get("observed_uptime_ms")
                                        if not number(at):continue
                                        age=capture/1000.-at
                                        name=sensor.get("id")
                                        old=sample["ranges"].get(name,{})
                                        if (sensor.get("valid") is True and sensor.get("status")=="ok"
                                                and number(sensor.get("range_m")) and 0<=age<=2000.*period
                                                and at>=old.get("observed_uptime_ms",-1)):
                                            sample["ranges"][name]=dict(distance=sensor["range_m"],
                                                sequence=sensor.get("sequence"),age_ms=age,observed_uptime_ms=at)
                            if previous and sample["frame_id"]!=previous["frame_id"]:
                                dt=sample["t"]-previous["t"]
                                if sample["boot"]!=previous["boot"] or generation!=previous.get("generation") or not 0<dt<=1.:
                                    model.reject("sensor_clock_gap")
                                    model.state["steering_known"]=False
                                    model.state["speed_m_s"]=None
                                    model.state["yaw_rate_deg_s"]=None
                                else:
                                    # Require one definitely applied state throughout
                                    # both capture intervals. Ambiguous transitions stay
                                    # logged for later replay, but cannot train online.
                                    active=[e for e in timeline if e["upper"]<=previous["lo"]]
                                    last=active[-1] if active else None
                                    changes=[e for e in timeline if
                                        e["upper"]>previous["lo"] and
                                        (e["lower"] is None or e["lower"]<=sample["hi"])]
                                    if not last or changes or last_ack is None or received-last_ack>0.5:
                                        model.reject("actuation_window_ambiguous")
                                        # Even unusable training windows carry steering
                                        # history. Replay every transition through the
                                        # latent memory; never reset it at neutral.
                                        cursor=previous["t"]
                                        prior=[e for e in timeline if e["upper"]<=cursor]
                                        mask=prior[-1]["mask"] if prior else 0
                                        changes_in_window=[e for e in timeline if
                                            e["upper"]>cursor and e["upper"]<=sample["t"]]
                                        for change in changes_in_window:
                                            at=(change["upper"]+change["lower"])/2. if change["lower"] is not None else change["upper"]
                                            at=max(cursor,min(sample["t"],at))
                                            model.advance(model.state,mask,at-cursor)
                                            cursor=at
                                            mask=change["mask"]
                                        model.advance(model.state,mask,max(0.,sample["t"]-cursor))
                                        model.state["steering_known"]=False
                                    else:
                                        ds,qualification=metric_delta(previous,sample)
                                        if ds is None:model.reject(qualification)
                                        model.observe(last["mask"],dt,wrap(yaw-previous["yaw"]),ds,
                                            dict(before_frame=previous["frame_id"],after_frame=sample["frame_id"],
                                                 capture_interval=[previous["lo"],sample["hi"]],
                                                 metric_qualification=qualification))
                            previous=sample
                            # Drop only in-memory history already older than the
                            # earliest frame; all original events remain in SQLite.
                            old=[e for e in timeline if e["upper"]<lo-2.]
                            if old:timeline=[old[-1]]+[e for e in timeline if e["upper"]>=lo-2.]
                exported=model.export()
                db.execute("INSERT OR REPLACE INTO model VALUES (1,?,?)",
                           (json.dumps(exported,allow_nan=False),time.time()))
                db.commit()
                self.latest_model=exported
                self.snapshot=dict(model.status(),recorder=dict(
                    path=str(self.path),pending=self.pending,error=None,
                    source="passive_applied_control_and_capture_associated_sensor_events"))
            db.commit()
            db.close()
        except Exception as error:
            self.error=type(error).__name__+": "+str(error)
            logging.exception("Rover motion recorder failed; control streams remain independent")

    def status(self):
        result=copy.deepcopy(self.snapshot) if self.snapshot else dict(ready=False)
        result["recorder_health"]=dict(pending=self.pending,error=self.error,alive=self.thread.is_alive())
        return result

    def predict(self,pulses,initial_state=None,step_s=.05):
        saved=copy.deepcopy(self.latest_model)
        if saved is None:
            raise RuntimeError("Motion recorder not initialized")
        model=MotionModel(saved)
        # Reconstruction for a read-only prediction must preserve the actual
        # live state, unlike process-restart recovery.
        model.state=copy.deepcopy(saved["state"])
        return model.predict(pulses,initial_state,step_s)

    def close(self):
        self.events.put(None)
        self.thread.join()

def install_motion(rover,app,home):
    """Attach once to an authenticated paired Rover app; never owns a lease."""
    from aiohttp import web
    from .stream_router import decode
    if getattr(rover,"motion_recorder",None):
        raise RuntimeError("Motion observer already installed")
    recorder=MotionRecorder(pathlib.Path(home)/"motion"/"observations.sqlite")
    rover.motion_recorder=recorder
    broadcast=rover.broadcast
    desired=rover.desired
    def observe(lane,message):
        received=time.monotonic()
        try:
            if lane=="control":
                value=json.loads(message) if isinstance(message,(str,bytes)) else dict(message)
                if isinstance(value,dict):
                    value=dict(value)
                    issued=value.get("issued_utc_ms")
                    if number(issued):
                        value["host_issue_age_s"]=max(0.,time.time()-issued/1000.)
                    recorder.offer("control",value,received)
            elif lane=="vision":
                metadata,unused=decode(message,4194304)
                metadata=metadata.get("snapshot",metadata)
                recorder.offer("vision",{k:metadata[k] for k in
                    ("frame","frame_id","spatial_evidence","ultrasonic","localization","robot_pose","power")
                    if k in metadata},received)
        except Exception:
            logging.exception("Invalid motion observation")
        return broadcast(lane,message)
    def request(mask,*args,**kwargs):
        import uuid
        identity=uuid.uuid4().hex
        recorder.offer("requested",dict(request_id=identity,mask=mask))
        try:
            receipt=desired(mask,*args,**kwargs)
        except Exception as error:
            recorder.offer("request_error",dict(request_id=identity,error=type(error).__name__,message=str(error)))
            raise
        recorder.offer("request_receipt",dict(request_id=identity,receipt=receipt))
        return receipt
    rover.broadcast=observe
    rover.desired=request
    async def status(request):
        return web.json_response(recorder.status())
    async def predict(request):
        try:
            body=await request.json()
            if not isinstance(body,dict) or set(body)-{"pulses","initial_state","step_s"}:
                raise ValueError("Expected pulses, optional initial_state and step_s")
            result=await asyncio.to_thread(recorder.predict,body.get("pulses"),
                    body.get("initial_state"),body.get("step_s",.05))
            return web.json_response(result)
        except (ValueError,TypeError,OverflowError) as error:
            raise web.HTTPBadRequest(text=str(error))
        except RuntimeError as error:
            raise web.HTTPServiceUnavailable(text=str(error))
    app.router.add_get("/api/motion/model",status)
    app.router.add_post("/api/motion/predict",predict)
    async def monitor():
        connected=None
        while True:
            now=bool(rover.control_ready)
            if now!=connected:
                recorder.offer("gap",dict(reason="control_binding_change",ready=now))
                connected=now
            await asyncio.sleep(.1)
    async def sonar_reader():
        import http.client
        import socket
        class UnixHTTP(http.client.HTTPConnection):
            def connect(self):
                self.sock=socket.socket(socket.AF_UNIX)
                self.sock.settimeout(5.)
                self.sock.connect(str(pathlib.Path.home()/".local/share/biome-robot-vision/vision.sock"))
        def query():
            connection=UnixHTTP("localhost")
            try:
                connection.request("POST","/v1/query",json.dumps(dict(operation="robot.sonar.snapshot",inputs={})),{"Content-Type":"application/json"})
                response=connection.getresponse()
                value=json.load(response)
                if response.status==200:return value
                return dict(availability="unavailable",http_status=response.status)
            finally:connection.close()
        while True:
            started=time.monotonic()
            try:
                value=await asyncio.to_thread(query)
                recorder.offer("sonar",dict(value,query_started_monotonic=started))
                interval=value.get("update_rate",{}).get("nominal_per_sensor_period_s") or .26
            except Exception as error:
                recorder.offer("sensor_error",dict(sensor="sonar",error=type(error).__name__))
                interval=1.
            await asyncio.sleep(interval)
    monitor_task=None
    sonar_task=None
    async def start(app):
        nonlocal monitor_task,sonar_task
        monitor_task=asyncio.create_task(monitor())
        sonar_task=asyncio.create_task(sonar_reader())
    async def stop(app):
        for task in (monitor_task,sonar_task):
            if task:task.cancel()
        await asyncio.gather(*[t for t in (monitor_task,sonar_task) if t],return_exceptions=True)
        recorder.offer("gap",dict(reason="server_shutdown"))
        await asyncio.to_thread(recorder.close)
    app.on_startup.append(start)
    app.on_cleanup.append(stop)
    return recorder
