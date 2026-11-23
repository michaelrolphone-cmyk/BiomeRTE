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
from rear_return_path import RearReturnPathCS5

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
                    resolution=row.get("resolution",64)
                    if resolution not in (16,64) or not isinstance(values,list) or len(values)!=resolution or any(type(v) is not int for v in values):
                        raise ValueError("rear_lidar_grid")
    return packet


from sensor_delivery import RecentReadings, freshness_window


class RearSensorIngress:
    def __init__(self,rig):
        self.rig=rig
        self.service=rig.service
        self.stop=threading.Event()
        self.thread=self.scheduler=self.device_thread=None
        self.clock_snapshot=None
        self.device_error=None
        self.loop=None
        self.config=None
        self.error=None
        self.latest=None
        self.lock=threading.Lock()
        self.packets=self.gaps=0
        self.ranging_error=None
        self.expired_packets=0
        self.connection_closes=0
        self.return_path_cs5_attempts=0
        self.return_path_cs5_last={}
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
            if self.config.get("gateway_host"):
                self.device_thread=threading.Thread(target=self._configure_device,name="rear-device-setup",daemon=True)
                self.device_thread.start()
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
        if getattr(self.service,"lidar",None) is not None:
            self.service.lidar.enqueue(packet,interval)
            metadata["lidar_fusion"]=self.service.lidar.status()
        self.service.rear_live_frames.publish(metadata,b"")
        self.error=None

    def capture_interval(self,packet,received):
        with self.lock:
            snapshot=self.clock_snapshot
        if not snapshot or packet.get("receiver_id")!=self.config["receiver_id"] or packet.get("boot_id")!=snapshot[0]:
            return None
        boot,bounds,observed=snapshot
        drift=abs(received-observed)*.0001
        tick=packet["capture_monotonic_us"]/1e6
        lower,upper=tick+bounds[0]-drift,tick+bounds[1]+drift
        if lower>received or upper<lower:return None
        return [lower,min(upper,received)]

    def _configure_device(self):
        from camera_telemetry import CameraHTTPClient
        import hashlib
        desired={"host":self.config["gateway_host"],"port":self.config.get("port",9878),"token":self.config["token"]}
        binding=hashlib.sha256(json.dumps([self.config["receiver_id"],desired],sort_keys=True).encode()).hexdigest()
        receipt=self.service.state_dir/"rear-socket-enrollment.json"
        previous=None
        try:
            previous=json.loads(receipt.read_text()).get("binding")
        except (OSError,ValueError):pass
        if previous==binding:return
        while not self.stop.wait(2):
            try:
                with self.lock:
                    enrolled=self.latest is not None and previous is None
                if not enrolled:
                    # First enrollment only. Authenticate the registered role before
                    # provisioning its destination. Boot or missing frames do not
                    # invalidate the socket configuration stored in the ESP's NVS.
                    client=CameraHTTPClient(self.rig.config["rear"]["camera"],{"network_timeout_s":2.})
                    status=client._request("/api/rear")
                    if status.get("receiver_id")!=self.config["receiver_id"] or status.get("camera_id")!="rear":
                        raise ValueError("rear_device_identity_mismatch")
                    client._request("/api/telemetry/stream",body=json.dumps(desired).encode())
                from vision_config import atomic_json
                atomic_json(receipt,{"binding":binding})
                self.device_error=None
                return
            except Exception as exc:
                self.device_error=type(exc).__name__+":"+str(exc)[:100]

    def _observe_route(self, receiver_id, host):
        from device_routes import DeviceRoutes
        registry = DeviceRoutes(self.service.state_dir / "device-routes.sqlite3")
        registry.observe(receiver_id, host, {"kind": "camera"})

    async def serve(self):
        from websockets.asyncio.server import serve
        self.loop=asyncio.get_running_loop()
        async def authorize(connection,request):
            if request.path not in ("/sensors","/camera") or not hmac.compare_digest(
                request.headers.get("Authorization",""),"Sensor "+self.config["token"]):
                return connection.respond(HTTPStatus.UNAUTHORIZED,"Unauthorized sensor source")
        from rear_transport_preview import messages_with_idle_recovery
        readings=RecentReadings(freshness_window(self.service))
        # This device already exchanges clock probes once per second. A delayed
        # protocol PONG must not terminate a connection carrying real readings.
        idle_seconds=max(12.,6*freshness_window(self.service))
        def deliver():
            while True:
                reading=readings.get()
                if reading is None:
                    return
                _, arguments=reading
                try:
                    self.publish(*arguments)
                except (ValueError, TypeError, KeyError) as exc:
                    self.error="rear_packet:"+str(exc)[:120]
        worker=threading.Thread(target=deliver, name="rear-sensor-delivery",daemon=True)
        worker.start()
        async def receive(socket):
            role = {"/camera": "camera", "/sensors": "sensors"}.get(
                getattr(getattr(socket, "request", None), "path", None))
            return_path = RearReturnPathCS5(enabled=self.config.get("return_path_cs5_trial"),
                                            receiver_id=self.config["receiver_id"], role=role)
            def after_validated(packet):
                evidence = return_path.after_validated_packet(socket, packet)
                if evidence is not None:
                    with self.lock:
                        self.return_path_cs5_attempts += 1
                        self.return_path_cs5_last[role] = evidence
            if role == "camera":
                from camera_push import receive_camera
                def deliver(packet,jpeg,interval,received):
                    camera=self.rig.camera
                    if camera is not None:
                        camera.push_jpeg(packet,jpeg,interval,received)
                from rear_transport_preview import publish_image
                # receive_camera validates identity and the JPEG before either
                # callback. Apply the opt-in socket option on this event loop.
                def deliver_validated(packet, *args):
                    after_validated(packet)
                    return asyncio.to_thread(deliver, packet, *args)
                def preview_validated(packet, *args):
                    after_validated(packet)
                    return asyncio.to_thread(publish_image, self, packet, *args)
                # A slow JPEG consumer can hold up its own acknowledgement only.
                await receive_camera(socket, self.config['receiver_id'], 'rear', ClockAlignment(),
                    deliver_validated,
                    messages=messages_with_idle_recovery(socket, idle_seconds),
                    boot_hint=lambda: getattr(self, 'last_wire_boot', None),
                    preview=preview_validated)
                return
            clock=ClockAlignment()
            # Prior bounds are usable across TCP reconnects for the same device boot.
            with self.lock:
                if self.clock_snapshot:
                    clock.boot, clock.bounds, clock.observed = self.clock_snapshot
            pending={}
            async def synchronize():
                while True:
                    sent=time.monotonic()
                    identity=str(time.monotonic_ns())
                    # A repeated probe must keep its own host send time.
                    # Never attribute the latest echo to the first retry.
                    pending[identity]=sent
                    for old in list(pending)[:-16]:pending.pop(old,None)
                    await socket.send(json.dumps({"type":"clock","id":identity}))
                    await asyncio.sleep(1)
            task=asyncio.create_task(synchronize())
            try:
                async for message in messages_with_idle_recovery(socket,idle_seconds):
                    received=time.monotonic()
                    packet=json.loads(message)
                    if packet.get("type")=="clock":
                        sent=pending.pop(packet.get("id"),None)
                        if sent is not None:
                            clock.observe(packet["boot_id"],sent,received,
                                packet["received_monotonic_us"],packet["served_monotonic_us"])
                            self.clock_round_trip_s=received-sent
                            with self.lock:self.clock_snapshot=(clock.boot,tuple(clock.bounds),clock.observed)
                        continue
                    validate_packet(packet,self.config["receiver_id"])
                    after_validated(packet)
                    self.last_wire_tick=time.monotonic()
                    self.last_wire_boot=packet["boot_id"]
                    peer=getattr(socket,"remote_address",None)
                    if peer and not getattr(socket,"_route_registered",False):
                        await asyncio.to_thread(self._observe_route, packet["receiver_id"], peer[0])
                        socket._route_registered=True
                    self.last_delivery_mode=packet.get("delivery_mode","legacy_ack")
                    if clock.boot!=packet["boot_id"]:
                        from rear_transport_preview import publish_sensor
                        publish_sensor(self,packet,received)
                        if self.last_delivery_mode!="continuous":
                            await socket.send(json.dumps({"type":"ack","boot_id":packet["boot_id"],
                                "stream_sequence":packet["stream_sequence"],"monotonic_us":packet["capture_monotonic_us"]}))
                        continue
                    interval=clock.capture_interval(packet,received)
                    self.last_capture_age_s=received-interval[0]
                    if packet.get("delivery_mode")=="continuous":
                        readings.put(interval[0], (packet,interval,received))
                        self.expired_packets=readings.expired
                        continue
                    await asyncio.to_thread(self.publish, packet, interval, received)
                    await socket.send(json.dumps({"type":"ack","boot_id":packet["boot_id"],
                        "stream_sequence":packet["stream_sequence"],
                        "monotonic_us":packet["capture_monotonic_us"]}))
            except Exception as exc:
                self.connection_closes+=1
                self.error="rear_socket:"+type(exc).__name__+":"+str(exc)[:160]
            finally:
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)
        try:
            async with serve(receive,self.config.get("listen","0.0.0.0"),self.config.get("port",9878),
                             process_request=authorize,max_size=None,max_queue=None,ping_interval=None) as server:
                self.bound_port=server.sockets[0].getsockname()[1]
                while not self.stop.is_set():
                    await asyncio.sleep(.1)
        finally:
            readings.close()
            await asyncio.to_thread(worker.join)

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
            return_path_cs5={"enabled":bool(self.config and self.config.get("return_path_cs5_trial") is True),
                             "attempts":self.return_path_cs5_attempts,
                             "last":copy.deepcopy(self.return_path_cs5_last)}
        now=time.monotonic()
        return {"configured":self.config is not None,"packets":self.packets,"gaps":self.gaps,
            "error":self.error,"ranging_error":self.ranging_error,"device_setup_error":self.device_error,
            "worker_alive":bool(self.thread and self.thread.is_alive()),
            "expired_packets":self.expired_packets,"connection_closes":self.connection_closes,
            "last_wire_age_s":now-self.last_wire_tick if hasattr(self,"last_wire_tick") else None,
            "last_wire_boot":getattr(self,"last_wire_boot",None),"delivery_mode":getattr(self,"last_delivery_mode",None),"clock_round_trip_s":getattr(self,"clock_round_trip_s",None),
            "last_capture_age_upper_s":getattr(self,"last_capture_age_s",None),
            "last_receive_age_s":None if latest is None else now-latest["received"],
            "latest":latest,"camera_id":"rear","return_path_cs5_trial":return_path_cs5}

    def close(self):
        self.stop.set()
        for thread in (self.thread,self.scheduler,self.device_thread):
            if thread:
                thread.join(timeout=3)
