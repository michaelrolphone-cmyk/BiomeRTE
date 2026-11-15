import asyncio
import json
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "providers/robot-vision"))
from biomerte.stream_router import Session, decode, frame
from biomerte.vision_mailbox import VisionMailbox
from rover_wire import SCHEMAS, catalog, envelope
from sensor_inventory import SensorInventory


def message(kind, sequence, **fields):
    return frame(envelope(dict(type=kind, sequence=sequence, authority="controller",
                               generation=1, epoch="one", **fields), "vision.test"),
                 b"image" if kind == "frame" else b"")


class WireContract(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_and_late_attach_have_an_ordered_prefix_under_frame_pressure(self):
        manifest = message("sensor_manifest", 100, sensors=[], revision="a")
        world = message("world_snapshot", 8, data={"map":{"map_id":"w","revision":3,"metadata":{}}})
        for incoming in ((world, manifest), (manifest, world)):
            box = VisionMailbox()
            box.put_nowait(message("frame", 3, frame_id="early"))
            for control in incoming:
                box.put_nowait(control)
            for i in range(101, 200):
                box.put_nowait(message("frame", i, frame_id=str(i)))
            prefix = [decode(await box.get())[0] for _ in range(3)]
            self.assertEqual([m["type"] for m in prefix], ["sensor_manifest","world_snapshot","frame"])
            self.assertEqual(prefix[-1]["frame_id"], "199")
            self.assertEqual(sorted(set(m["sequence"] for m in prefix)), [m["sequence"] for m in prefix])
            box.put_nowait(message("sensor_manifest", 201, sensors=[], revision="b"))
            self.assertEqual(decode(await box.get())[0]["revision"], "b")

    async def test_waiter_does_not_receive_an_image_before_world(self):
        box = VisionMailbox()
        box.put_nowait(message("sensor_manifest", 4, sensors=[], revision="a"))
        await box.get()
        pending = asyncio.create_task(box.get())
        box.put_nowait(message("frame", 5, frame_id="f"))
        await asyncio.sleep(0)
        self.assertFalse(pending.done())
        box.put_nowait(message("world_snapshot", 1, data={"map":{}}))
        self.assertEqual(decode(await pending)[0]["type"], "world_snapshot")

    def test_resource_routing_preserves_measurements_and_image(self):
        raw = {"type":"frame", "tof":{"camera":"http://192.168.1.45", "anchors":[{"id":"hall","ip":"192.168.1.100","range_m":2.3}],
               "anchor_transport":{"anchors":[{"ip":"10.2.1.9","valid":True}]}},
               "pose":{"reason":"timeout connecting to rtsp://10.0.0.1:9554/camera"}}
        result = envelope(raw, "vision.rover")
        self.assertEqual("vision.rover", result["tof"]["camera"])
        self.assertEqual({"id":"hall","range_m":2.3},result["tof"]["anchors"][0])
        self.assertNotRegex(json.dumps(result), r'http://|(?:\d{1,3}\.){3}\d{1,3}')
        self.assertEqual(raw["tof"]["anchors"][0]["ip"], "192.168.1.100")

    def test_catalog_covers_each_emitted_type(self):
        schemas = catalog()["schemas"]
        self.assertEqual(set(schemas),set(SCHEMAS.values()))
        for kind, schema in SCHEMAS.items():
            self.assertEqual(kind, schemas[schema]["properties"]["type"]["const"])
            self.assertEqual(schema, catalog(schema)["properties"]["schema"]["const"])

    def test_inventory_discovers_generic_device_and_lidar_without_reporting_replays_as_new(self):
        import time
        from types import SimpleNamespace
        now = time.monotonic()
        service = SimpleNamespace(_robot_model=lambda:{"definition":{}},
                                  camera_rig=SimpleNamespace(config={"rear":{"enabled":True,"camera":{}}}),
                                  config={"camera":{"expected_fps":5}})
        import tempfile,threading
        from lidar_fusion import defaults
        from sensor_rig_registry import configured_sensors
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        service.state_dir=pathlib.Path(temporary.name)
        service.lidar=SimpleNamespace(lock=threading.RLock(),config=defaults(),path=service.state_dir/"sensor-rig.json")
        service.camera_rig.config["rear"]["T_front_camera_rear_camera_m"]=[[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]
        configured_sensors(service)
        service.lidar.config["sensors"].append({"id":"ambient.probe","type":"temperature","role":"outside"})
        service.lidar.path.write_text(json.dumps(service.lidar.config))
        inventory = SensorInventory()
        meta = {"camera_id":"rear","processed_monotonic":now,"frame_id":"rear#1",
                "sensor_packet":{"boot_id":"boot","stream_sequence":1,
                                 "sensors":[{"id":"ambient.probe","type":"temperature","role":"outside"}]},
                "lidar":{"latest":{"received_monotonic":now-30,
                    "sensors":[{"sensor_id":"rear","raw":{"sequence":12,"ready":True}}]}}}
        inventory.observe(meta,b"jpeg")
        first = inventory.snapshot(service)
        second = inventory.snapshot(service)
        self.assertEqual(first["revision"],second["revision"])
        wire = envelope(first, "vision.rover")
        rows = {s["id"]:s for s in wire["sensors"]}
        self.assertEqual(rows["camera.rear"]["status"],"online")
        self.assertEqual(rows["lidar.rear"]["status"],"offline")
        self.assertEqual(rows["ambient.probe"]["stream"]["resource"],"vision.rover")
        self.assertEqual(rows["ambient.probe"]["stream"]["path"],"sensor_packet.sensors")
        self.assertIsNone(rows["camera.front"]["last_seen"])
