"""Rear socket ingress using the front sensor protocol, without front AHRS mutation.

All publications carry the existing service's map identity. No map or SLAM backend
is constructed here. The optional ranging scheduler never runs on either camera's
tracking thread.
"""
import asyncio
import copy
import hmac
import json
import math
import re
import threading
import time
from http import HTTPStatus

class ClockAlignment:
    def __init__(self):
        self.boot = None
        self.bounds = None
        self.observed = None

    def observe(self, boot, sent, received, device_received, device_sent):
        if (not isinstance(boot, str) or not boot or
                type(device_received) is not int or type(device_sent) is not int or
                not 0 <= device_received <= device_sent or received < sent):
            raise ValueError('Invalid clock exchange')
        lower, upper = sent-device_sent/1e6, received-device_received/1e6
        if upper < lower:
            raise ValueError('Invalid clock interval')
        if self.boot == boot and self.bounds is not None:
            drift=max(0.,received-self.observed)*.0001
            shared=(max(lower,self.bounds[0]-drift),min(upper,self.bounds[1]+drift))
            if shared[0]<=shared[1]:lower,upper=shared
        self.boot, self.bounds, self.observed = boot, (lower, upper), received

    def capture_interval(self, packet, received):
        if self.boot != packet['boot_id'] or self.bounds is None:
            raise ValueError('Clock not aligned to sensor boot')
        # Crystal drift broadens the interval between actual clock exchanges.
        drift = max(0., received-self.observed)*.0001
        tick = packet['capture_monotonic_us']/1e6
        lower, upper = tick+self.bounds[0]-drift, tick+self.bounds[1]+drift
        if lower > received:
            raise ValueError('Sensor timestamp is in the future')
        return [lower, min(upper, received)]


def validate_packet(packet, receiver_id):
    if not isinstance(packet,dict) or packet.get("schema")!="biome.sensor_tick.v1":
        raise ValueError("rear_sensor_schema")
    if packet.get("receiver_id")!=receiver_id or packet.get("camera_id")!="rear":
        raise ValueError("rear_sensor_identity")
    for key in ("receiver_id","boot_id","frame_id"):
        if not isinstance(packet.get(key),str) or not packet[key]:
            raise ValueError("rear_sensor_identity")
    tick=packet.get("capture_monotonic_us")
    served=packet.get("served_monotonic_us")
    if (type(tick) is not int or tick<0 or type(served) is not int or served<tick or
        packet.get("capture_clock")!="esp_timer" or
        packet["frame_id"]!=packet["boot_id"]+":"+str(tick)):
        raise ValueError("rear_sensor_clock")
    if (packet.get("image_available") is not False or
        packet.get("capture_timestamp_semantics")!="sensor_snapshot_tick_no_camera_frame"):
        raise ValueError("rear_sensor_tick_is_not_image")
    if type(packet.get("stream_sequence")) is not int or packet["stream_sequence"]<1:
        raise ValueError("rear_sensor_sequence")
    # Reject invented/future/stale ranges even when a sender claims validity.
    lidar=packet.get("lidar")
    if lidar is not None:
        if not isinstance(lidar,dict) or not isinstance(lidar.get("sensors"),list):
            raise ValueError("rear_lidar_schema")
        for row in lidar["sensors"]:
            if row.get("valid"):
                observed=row.get("observed_monotonic_us")
                if type(observed) is not int or not 0<=observed<=tick or tick-observed>1000000:
                    raise ValueError("rear_lidar_timestamp")
                for field in ("distance_mm","target_status"):
                    values=row.get(field)
                    if not isinstance(values,list) or len(values)!=64 or any(type(v) is not int for v in values):
                        raise ValueError("rear_lidar_grid")
    return packet


class RearSensorIngress:
    def __init__(self,rig):
        self.rig=rig
        self.service=rig.service
        self.stop=threading.Event()
        self.thread=self.scheduler=None
        self.loop=None
        self.config=None
        self.error=None
        self.latest=None
        self.lock=threading.Lock()
        self.packets=self.gaps=0
        self.ranging_error=None
        path=self.service.state_dir/"rear-sensor-push.json"
        try:
            if path.exists():
                value=json.loads(path.read_text())
                if (not isinstance(value.get("token"),str) or len(value["token"])<32 or
                    not isinstance(value.get("receiver_id"),str) or not value["receiver_id"] or
                    type(value.get("port",9878)) is not int or not 1<=value.get("port",9878)<=65535):
                    raise ValueError("rear_sensor_configuration")
                for peer in value.get("ftm_peers",[]):
                    if (not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}",peer["address"]) or
                        int(peer["address"][:2],16)&1 or
                        type(peer["channel"]) is not int or not 1<=peer["channel"]<=14):
                        raise ValueError("rear_ftm_peer")
                self.config=value
        except (ValueError,KeyError,TypeError,OSError):
            self.error="rear_sensor_configuration_invalid"

    def start(self):
        if self.config and not self.thread:
            self.thread=threading.Thread(target=self._run,name="rear-sensor-websocket",daemon=True)
            self.thread.start()
            if self.config.get("ftm_peers"):
                self.scheduler=threading.Thread(target=self._range,name="rear-ftm-schedule",daemon=True)
                self.scheduler.start()
        return self

    def publish(self,packet,interval,received):
        packet=validate_packet(packet,self.config["receiver_id"])
        if (len(interval)!=2 or not all(math.isfinite(v) for v in interval) or
            interval[0]>interval[1] or interval[1]>received):
            raise ValueError("rear_capture_interval")
        with self.lock:
            previous=self.latest
            if previous and previous["packet"]["boot_id"]==packet["boot_id"]:
                old=previous["packet"]
                if packet["stream_sequence"]<=old["stream_sequence"] or packet["capture_monotonic_us"]<=old["capture_monotonic_us"]:
                    raise ValueError("replayed_rear_sensor_packet")
                self.gaps+=max(0,packet["stream_sequence"]-old["stream_sequence"]-1)
            self.latest={"packet":copy.deepcopy(packet),"interval":list(interval),"received":received}
            self.packets+=1
        metadata={
            "type":"telemetry","camera_id":"rear","source_id":"rear-camera",
            "frame_id":packet["frame_id"],"sensor_tick":True,"image_available":False,
            "sensor_delivery":"websocket_push","sensor_packet":packet,
            "capture_host_monotonic_interval_s":list(interval),
            "sensor_clock_uncertainty_s":(interval[1]-interval[0])/2,
            "capture_age_upper_s":received-interval[0],
            "sensor_stream_sequence":packet["stream_sequence"],
            "processed_monotonic":received,"processed_monotonic_basis":"sensor_packet_receive_time",
            "map_id":self.service.map_id,"map_revision":self.service.revision,
            "session_id":self.service.session_id,
            "lidar":packet.get("lidar"),"ftm":packet.get("ftm"),
            "ftm_responder":packet.get("ftm_responder"),"uart":packet.get("uart")}
        self.service.live_frames.publish(metadata,b"")
        self.error=None

    async def serve(self):
        from websockets.asyncio.server import serve
        self.loop=asyncio.get_running_loop()
        async def authorize(connection,request):
            if request.path!="/sensors" or not hmac.compare_digest(
                request.headers.get("Authorization",""),"Sensor "+self.config["token"]):
                return connection.respond(HTTPStatus.UNAUTHORIZED,"Unauthorized sensor source")
        async def receive(socket):
            clock=ClockAlignment()
            pending={}
            async def synchronize():
                while True:
                    if not pending:
                        identity=str(time.monotonic_ns())
                        pending[identity]=time.monotonic()
                        await socket.send(json.dumps({"type":"clock","id":identity}))
                    await asyncio.sleep(1)
            task=asyncio.create_task(synchronize())
            try:
                async for message in socket:
                    received=time.monotonic()
                    packet=json.loads(message)
                    if packet.get("type")=="clock":
                        sent=pending.pop(packet.get("id"),None)
                        if sent is not None:
                            clock.observe(packet["boot_id"],sent,received,
                                packet["received_monotonic_us"],packet["served_monotonic_us"])
                        continue
                    validate_packet(packet,self.config["receiver_id"])
                    interval=clock.capture_interval(packet,received)
                    self.publish(packet,interval,received)
                    await socket.send(json.dumps({"type":"ack","boot_id":packet["boot_id"],
                        "stream_sequence":packet["stream_sequence"],
                        "monotonic_us":packet["capture_monotonic_us"]}))
            except Exception as exc:
                self.error="rear_socket:"+type(exc).__name__
            finally:
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)
        async with serve(receive,self.config.get("listen","0.0.0.0"),self.config.get("port",9878),
                         process_request=authorize,max_size=None,max_queue=1,ping_interval=15) as server:
            self.bound_port=server.sockets[0].getsockname()[1]
            while not self.stop.is_set():
                await asyncio.sleep(.1)

    def _run(self):
        try:
            asyncio.run(self.serve())
        except Exception as exc:
            # A rear listener failure never tears down the front camera service.
            self.error="rear_listener:"+type(exc).__name__

    def _range(self):
        from camera_telemetry import CameraHTTPClient
        index=0
        awaiting=None
        while not self.stop.wait(.25):
            with self.lock:
                latest=copy.deepcopy(self.latest)
            if not latest or time.monotonic()-latest["received"]>2:
                continue
            packet=latest["packet"]
            ftm=packet.get("ftm") or {}
            identity=(packet["boot_id"],ftm.get("report_id"))
            if ftm.get("busy") or ftm.get("retiring"):
                continue
            if awaiting is not None and identity==awaiting:
                continue
            peers=self.config["ftm_peers"]
            peer=peers[index%len(peers)]
            try:
                # The response only queues a session. Wait for a different report
                # identity on the socket before starting the next peer.
                client=CameraHTTPClient(self.rig.config["rear"]["camera"])
                client._request("/api/telemetry/ftm",body=json.dumps(peer).encode(),
                                success_statuses=(202,),known_rejections=(409,503))
                awaiting=identity
                index+=1
                self.ranging_error=None
            except Exception as exc:
                self.ranging_error=type(exc).__name__
                if getattr(exc,"outcome_known",True) is False:
                    awaiting=identity  # Reconcile a possibly queued operation on the socket.
                self.stop.wait(1)

    def status(self):
        with self.lock:
            latest=copy.deepcopy(self.latest)
        now=time.monotonic()
        return {"configured":self.config is not None,"packets":self.packets,"gaps":self.gaps,
            "error":self.error,"ranging_error":self.ranging_error,
            "worker_alive":bool(self.thread and self.thread.is_alive()),
            "last_receive_age_s":None if latest is None else now-latest["received"],
            "latest":latest,"camera_id":"rear"}

    def close(self):
        self.stop.set()
        for thread in (self.thread,self.scheduler):
            if thread:
                thread.join(timeout=3)
