import asyncio
import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from rear_sensor_push import ClockAlignment, RearSensorIngress, validate_packet

def packet(sequence=1,boot="boot-a",tick=1000000):
    return dict(schema="biome.sensor_tick.v1",camera_id="rear",receiver_id="rear-test",
        boot_id=boot,frame_id=boot+":"+str(tick),capture_monotonic_us=tick,
        served_monotonic_us=tick+1,capture_clock="esp_timer",stream_sequence=sequence,
        image_available=False,capture_timestamp_semantics="sensor_snapshot_tick_no_camera_frame",
        lidar={"schema":"rover.lidar.v1","sensors":[{"id":"front","valid":False},
               {"id":"rear","valid":False}]},ftm={"report_id":1,"busy":False,"status":"never_requested"},
        uart={"connected":False})

def rig(path):
    output=[]
    service=SimpleNamespace(state_dir=Path(path),map_id="ONE-MAP",revision=7,session_id="same-session",
                            live_frames=SimpleNamespace(publish=lambda *args:(_ for _ in ()).throw(AssertionError("rear telemetry entered front stream"))),
                            rear_live_frames=SimpleNamespace(publish=lambda meta,jpeg:output.append((meta,jpeg))),
                            backend=object(),ahrs=object())
    value=SimpleNamespace(service=service,config={"rear":{"camera":{"url":""}}})
    return value,output

class PacketTests(unittest.TestCase):
    def test_missing_peripherals_publish_into_existing_map_without_front_mutation(self):
        with tempfile.TemporaryDirectory() as d:
            r,out=rig(d);front=r.service.ahrs;backend=r.service.backend
            ingress=RearSensorIngress(r);ingress.config={"receiver_id":"rear-test"}
            ingress.publish(packet(),[99.,99.01],100.)
            self.assertEqual(len(out),1)
            self.assertEqual(out[0][0]["map_id"],"ONE-MAP")
            self.assertEqual(out[0][0]["camera_id"],"rear")
            self.assertFalse(out[0][0]["image_available"]);self.assertEqual(out[0][1],b"")
            self.assertIs(r.service.ahrs,front);self.assertIs(r.service.backend,backend)
    def test_replay_rejected_but_reboot_restarts_sequence(self):
        with tempfile.TemporaryDirectory() as d:
            r,out=rig(d);ingress=RearSensorIngress(r);ingress.config={"receiver_id":"rear-test"}
            ingress.publish(packet(),[99.,99.01],100.)
            with self.assertRaises(ValueError):ingress.publish(packet(),[99.,99.01],100.)
            ingress.publish(packet(3,tick=1000001),[99.,99.01],100.)
            self.assertEqual(ingress.gaps,1)
            ingress.publish(packet(1,boot="boot-b",tick=1),[99.,99.01],100.)
            self.assertEqual(len(out),3)
    def test_front_identity_and_image_claim_rejected(self):
        for field,value in (("receiver_id","front"),("camera_id","front"),("image_available",True),
                            ("stream_sequence",True),("served_monotonic_us",0)):
            p=packet();p[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):validate_packet(p,"rear-test")
    def test_future_stale_and_malformed_lidar_rejected(self):
        p=packet(tick=2000000)
        row={"id":"rear","valid":True,"observed_monotonic_us":2000000,
             "distance_mm":[100]*64,"target_status":[5]*64}
        p["lidar"]["sensors"]=[row]
        validate_packet(p,"rear-test")
        for observed in (2000001,999999):
            value=copy.deepcopy(p);value["lidar"]["sensors"][0]["observed_monotonic_us"]=observed
            with self.assertRaises(ValueError):validate_packet(value,"rear-test")
        p["lidar"]["sensors"][0]["distance_mm"]=[]
        with self.assertRaises(ValueError):validate_packet(p,"rear-test")
    def test_bad_configuration_is_isolated(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d,"rear-sensor-push.json").write_text('{"token":"bad"}')
            r,out=rig(d);ingress=RearSensorIngress(r)
            self.assertIsNone(ingress.config);self.assertEqual(ingress.error,"rear_sensor_configuration_invalid")
            ingress.start();self.assertIsNone(ingress.thread)
    def test_clock_is_boot_scoped_and_future_ticks_rejected(self):
        c=ClockAlignment();c.observe("boot-a",100.,100.02,1000000,1000000)
        interval=c.capture_interval(packet(),100.03)
        self.assertAlmostEqual(interval[0],100.,places=4)
        with self.assertRaises(ValueError):c.capture_interval(packet(boot="boot-b"),100.03)
        with self.assertRaises(ValueError):c.capture_interval(packet(tick=2000000),100.03)

class SocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.rig,self.output=rig(self.tmp.name)
        self.ingress=RearSensorIngress(self.rig)
        self.ingress.config={"receiver_id":"rear-test","listen":"127.0.0.1","port":0,"token":"x"*32}
        self.task=asyncio.create_task(self.ingress.serve())
        for _ in range(200):
            if hasattr(self.ingress,"bound_port"):break
            if self.task.done():await self.task
            await asyncio.sleep(.01)
        self.url="ws://127.0.0.1:"+str(self.ingress.bound_port)+"/sensors"
    async def asyncTearDown(self):
        self.ingress.stop.set()
        await asyncio.wait_for(self.task,3)
        self.tmp.cleanup()
    async def exchange(self,boot):
        from websockets.asyncio.client import connect
        async with connect(self.url,additional_headers={"Authorization":"Sensor "+"x"*32}) as ws:
            clock=json.loads(await asyncio.wait_for(ws.recv(),2))
            await ws.send(json.dumps(dict(type="clock",id=clock["id"],boot_id=boot,
                received_monotonic_us=1000000,served_monotonic_us=1000000)))
            await ws.send(json.dumps(packet(boot=boot)))
            ack=json.loads(await asyncio.wait_for(ws.recv(),2))
            self.assertEqual(ack,dict(type="ack",boot_id=boot,stream_sequence=1,monotonic_us=1000000))
    async def test_real_socket_clock_ack_disconnect_reboot_without_camera(self):
        await self.exchange("boot-a")
        await self.exchange("boot-b")
        self.assertEqual(len(self.output),2)
        self.assertEqual(self.output[-1][0]["sensor_delivery"],"websocket_push")
        self.assertEqual(self.output[-1][0]["map_id"],"ONE-MAP")
    async def test_socket_rejects_wrong_authentication(self):
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidStatus
        with self.assertRaises(InvalidStatus):
            async with connect(self.url,additional_headers={"Authorization":"Sensor wrong"}):
                pass

    async def test_lidar_delivers_while_jpeg_consumer_is_blocked(self):
        from websockets.asyncio.client import connect
        from test_capture_packet import packet as camera_packet
        entered, release = threading.Event(), threading.Event()
        callback_finished = []
        def publish(metadata, image):
            if image:
                entered.set()
                callback_finished.append(release.wait(3))
            self.output.append((metadata, image))
        self.rig.service.rear_live_frames.publish = publish
        frame = camera_packet()
        frame.update(receiver_id="rear-test", camera_id="rear")
        header = json.dumps(frame).encode()
        payload = len(header).to_bytes(4, "big") + header + bytes.fromhex("ffd8ffe0ffd9")
        async with connect(self.url.replace("/sensors", "/camera"),
                           additional_headers={"Authorization": "Sensor " + "x" * 32}) as camera:
            await camera.send(payload)
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                await self.exchange("independent-lidar")
                self.assertEqual(self.output[-1][0]["sensor_packet"]["boot_id"], "independent-lidar")
                self.assertEqual(callback_finished, [])
            finally:
                release.set()
            while True:
                reply = json.loads(await asyncio.wait_for(camera.recv(), 2))
                if reply["type"] == "camera_ack":
                    self.assertEqual(reply["frame_id"], frame["frame_id"])
                    break
        self.assertEqual(callback_finished, [True])

    async def test_route_registry_wait_does_not_block_other_socket_handshakes(self):
        from unittest.mock import patch
        from websockets.asyncio.client import connect
        from device_routes import DeviceRoutes
        entered, release = threading.Event(), threading.Event()
        original = DeviceRoutes.observe
        released_by_test = []
        def slow_observe(registry, *args):
            entered.set()
            released_by_test.append(release.wait(3))
            return original(registry, *args)
        with patch.object(DeviceRoutes, "observe", slow_observe):
            sensor = asyncio.create_task(self.exchange("route-wait"))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                async with connect(self.url.replace("/sensors", "/camera"),
                                   additional_headers={"Authorization": "Sensor " + "x" * 32}) as camera:
                    clock = json.loads(await asyncio.wait_for(camera.recv(), 2))
                    self.assertEqual(clock["type"], "clock")
                    self.assertEqual(released_by_test, [])
            finally:
                release.set()
                await sensor
        self.assertEqual(released_by_test, [True])

if __name__=="__main__":unittest.main()
