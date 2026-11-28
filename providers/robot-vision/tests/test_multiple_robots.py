"""Qualify two instances without live hardware or shared map state."""
import asyncio
import copy
import hashlib
import hmac
import json
from pathlib import Path
import queue
import socket
import tempfile
import time
from types import SimpleNamespace
import unittest

import numpy as np

from biome_provider import stream_offer, supported_contracts
from install_provider import job_labels, make_config
from lidar_fusion import defaults
from robot_motion import COMMAND, request
from robot_profiles import bind_robot_state, model_definition, robot_identity
from sensor_push import SensorPushReceiver
from test_vision_service import BackendFactory, FakeCamera, FakeDetector, calibration
from vision_config import DEFAULT_CONFIG
from vision_service import VisionService

SPIDER = {"robot_id": "spider", "model": "lafvin-spider"}
TOKEN = "spider-test-token-012345678901234567"
SECRET = "spider-test-motion-01234567890123456"


def packet(tick, sequence, receiver="spider-camera"):
    return {"schema": "biome.sensor_tick.v1", "receiver_id": receiver,
            "camera_id": "front", "boot_id": "spider-boot", "frame_id": f"spider-boot:{tick}",
            "capture_monotonic_us": tick, "served_monotonic_us": tick+1,
            "capture_clock": "esp_timer", "stream_sequence": sequence, "image_available": False,
            "capture_timestamp_semantics": "sensor_snapshot_tick_no_camera_frame",
            "delivery_mode": "continuous"}


def make_spider(path, port=9879):
    path.mkdir()
    config = {
        "robot": {"id": "spider", "model": "lafvin-spider"},
        "camera": {"source_id": "spider-camera", "capture_transport": "websocket_push"},
    }
    (path / "config.json").write_text(json.dumps(config))
    (path / "sensor-push.json").write_text(json.dumps({
        "receiver_id": "spider-camera", "listen": "127.0.0.1", "port": port,
        "token": TOKEN, "motion_enabled": True, "motion_secret": SECRET,
    }))
    return config


def sample_grid(name, tick, distance):
    return {"id": name, "valid": True, "resolution": 64, "observed_monotonic_us": tick,
            "distance_mm": [distance]*64, "target_status": [5]*64}


class MultipleRobotTest(unittest.TestCase):
    def test_models_store_and_provider_are_bound_to_each_robot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            rover_state, spider_state = path / "rover", path / "spider"
            config = make_spider(spider_state)
            rover_settings = copy.deepcopy(DEFAULT_CONFIG)
            rover_settings["camera"]["url"] = "fixture://rover"
            services = []
            try:
                for state, settings in [(rover_state, rover_settings), (spider_state, config)]:
                    service = VisionService(settings, state, camera=FakeCamera(),
                                            detector=FakeDetector(), backend_factory=BackendFactory())
                    services.append(service)
                rover, spider = services
                self.assertNotEqual(spider.map_id, rover.map_id)
                self.assertIsNot(spider.backend, rover.backend)
                self.assertEqual(spider.store.read_map(spider.map_id)["map"]["metadata"]["robot_id"], "spider")
                self.assertEqual(rover.query("robot.control.model")["channel_motion"]["1"], "forward")
                self.assertNotIn(COMMAND, rover.capabilities)
                self.assertIn(COMMAND, spider.capabilities)
                self.assertEqual(spider.query("robot.control.model")["transport"], "spider_command.v1")
                definition = spider.query("robot.body.model")["definition"]
                self.assertEqual(definition["geometry"]["catalog_envelope_m"], [0.17, 0.16, 0.09])
                self.assertIsNone(definition["geometry"]["calibrated_kinematics"])
                self.assertIsNone(definition["camera"]["T_robot_camera"])
                self.assertNotIn("rear", definition["cameras"])
                self.assertFalse(spider.query("robot.metric.status")["local_metric_geometry_available"])
                self.assertIsNone(spider.pose_estimator.mount)
                self.assertEqual(spider.sonar_mounts, {})
                self.assertIn("front", rover.sonar_mounts)
                self.assertFalse(list(spider_state.glob("sonar-obstacles.sqlite3*")))
                self.assertFalse(list(rover_state.glob("sonar-obstacles.sqlite3*")))
                self.assertEqual(spider.lidar.config["lidar"], {})
                self.assertIn("front", rover.lidar.config["lidar"])
                with self.assertRaisesRegex(ValueError, "another_robot"):
                    spider._check_map_robot(rover.store.read_map(rover.map_id)["map"])
                with self.assertRaisesRegex(ValueError, "another_robot"):
                    bind_robot_state(spider_state, robot_identity({}))
                with self.assertRaisesRegex(ValueError, "unbound_robot_state"):
                    bind_robot_state(rover_state, SPIDER)
                gateway, terminal = {"site": "fixture"}, {"node": "1"*32}
                first, second = [make_config(path, state, path, gateway, terminal)
                                 for state in (rover_state, spider_state)]
                self.assertNotEqual(first["resource"], second["resource"])
                self.assertNotEqual(first["provider"], second["provider"])
                self.assertTrue(set(job_labels(first)).isdisjoint(job_labels(second)))
                spider_status = spider.query("robot.vision.status", {"provider_only": True})
                advertised = stream_offer(second, spider_status)
                self.assertEqual(advertised["configuration"]["robot_id"], "spider")
                self.assertIn("robot.vision.project_ground:2", supported_contracts(
                    rover.query("robot.vision.status", {"provider_only": True})))
                self.assertNotIn("robot.vision.project_ground:2", supported_contracts(spider_status))
            finally:
                for service in reversed(services):
                    service.close()

    def test_spider_named_depth_sources_share_only_its_calibrated_camera(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)/"spider"
            config = make_spider(state)
            config["calibration"] = calibration("spider-fixture")
            service = VisionService(config, state, camera=FakeCamera(),
                                    detector=FakeDetector(), backend_factory=BackendFactory())
            try:
                lidar = service.lidar
                t = 1000000
                row = packet(t, 1)
                row["lidar"] = {"sensors": [sample_grid("spider-tof-0", t, 2000),
                                           sample_grid("spider-tof-1", t, 1000)]}
                now = time.monotonic()
                image = {"sensor_packet": row, "capture_monotonic_us": t}
                lidar.enqueue(row, [now, now])
                self.assertIsNone(lidar.for_camera(image, "front", service.calibration))
                with self.assertRaisesRegex(ValueError, "robot_mismatch"):
                    lidar.configure(defaults())
                mount = {**defaults()["lidar"]["front"], "validated": True,
                         "T_front_camera_sensor_m": np.eye(4).tolist()}
                rig = {"schema": "rover.sensor-rig/1", "robot_id": "spider", "revision": 2,
                       "lidar": {"spider-tof-0": mount, "spider-tof-1": mount}}
                lidar.configure(rig)
                lidar.enqueue({**row, "receiver_id": "other-camera"}, [now, now])
                self.assertIsNone(lidar.for_camera(image, "front", service.calibration))
                lidar.enqueue(row, [now, now])
                result = lidar.for_camera(image, "front", service.calibration)
                self.assertEqual(result["sensor_ids"], ["spider-tof-0", "spider-tof-1"])
                mapped = result["depth"] > 0
                self.assertTrue(mapped.any())
                self.assertTrue((result["depth"][mapped] == 1).all())
                self.assertTrue((result["zones"][mapped] >= 64).all())
                self.assertEqual(result["map_id"], service.map_id)
                lidar.configure(rig)
                one_grid = {**row, "lidar": {"sensors": row["lidar"]["sensors"][1:]}}
                lidar.enqueue(one_grid, [now, now])
                result = lidar.for_camera(image, "front", service.calibration)
                self.assertEqual(result["sensor_ids"], ["spider-tof-1"])
                self.assertTrue((result["zones"][result["depth"] > 0] >= 64).all())
                rig["lidar"]["spider-tof-0"] = {**mount, "validated": False}
                with self.assertRaisesRegex(ValueError, "calibrated_lidar_mount_required"):
                    lidar.configure(rig)
            finally:
                service.close()

    def test_spider_rearward_grid_maps_without_a_rear_camera(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "spider"
            config = make_spider(state)
            config["calibration"] = calibration("opposed-grids-fixture")
            service = VisionService(config, state, camera=FakeCamera(),
                                    detector=FakeDetector(), backend_factory=BackendFactory())
            try:
                lidar = service.lidar
                front = {**defaults()["lidar"]["front"], "validated": True,
                         "T_front_camera_sensor_m": np.eye(4).tolist()}
                rear_matrix = np.diag([-1., 1., -1., 1.])
                rear = {**front, "projection_camera_id": None,
                        "T_front_camera_sensor_m": rear_matrix.tolist()}
                rig = {"schema": "rover.sensor-rig/1", "robot_id": "spider", "revision": 2,
                       "lidar": {"spider-tof-0": front, "spider-tof-1": rear}}
                lidar.configure(rig)
                t = 1000000
                row = packet(t, 1)
                row["lidar"] = {"sensors": [sample_grid(name, t, 1300) for name in rig["lidar"]]}
                now = time.monotonic()
                interval = [now, now]
                image = {"sensor_packet": row, "capture_monotonic_us": t}
                lidar.enqueue(row, interval)
                result = lidar.for_camera(image, "front", service.calibration)
                self.assertEqual(result["sensor_id"], "spider-tof-0")
                self.assertIsNone(lidar.for_camera(image, "rear", service.calibration))
                lidar.process(row, interval, service.map_id)
                mapped = lidar.latest["sensors"]
                self.assertEqual([scan["sensor_id"] for scan in mapped], list(rig["lidar"]))
                for scan, sign in zip(mapped, (1, -1)):
                    optical = scan["points"][0]["xyz_sensor_m"]
                    front_axes = np.asarray(scan["mount"]["T_front_camera_sensor_m"]) @ [*optical, 1.]
                    self.assertGreater(sign * front_axes[2], 0.)

                # An independently calibrated rear camera can use the rear
                # depth grid without changing either grid's acquisition peer.
                rear["projection_camera_id"] = "rear"
                lidar.configure(rig)
                lidar.enqueue(row, interval)
                depth = lidar.for_camera(image, "rear", service.calibration, rear_matrix)
                self.assertEqual(depth["sensor_id"], "spider-tof-1")
                self.assertGreater(depth["depth"].max(), 0.)
            finally:
                service.close()


class MotionRouteTest(unittest.IsolatedAsyncioTestCase):
    async def test_signed_motion_uses_the_bound_spawned_socket(self):
        from websockets.asyncio.client import connect
        readings = queue.Queue()
        with tempfile.TemporaryDirectory() as directory:
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            state = Path(directory)/"spider"
            make_spider(state, port)
            service = SimpleNamespace(state_dir=state, config={}, capabilities=[COMMAND],
                                      robot_identity=SPIDER, robot_definition=model_definition(state, SPIDER))

            def publish(packet, *args, capture_interval):
                stream._push_snapshot = packet, capture_interval
                readings.put(packet)

            stream = SimpleNamespace(service=service, error=None, _push_snapshot=None, publish=publish)
            service.sensor_ticks = stream
            stream.push = receiver = SensorPushReceiver(stream)
            receiver.start()
            try:
                deadline = time.monotonic()+5
                status = state/"sensor-push-status.json"
                while not status.exists():
                    self.assertLess(time.monotonic(), deadline)
                    await asyncio.sleep(.01)
                headers = {"Authorization": "Sensor " + TOKEN}
                async with connect(f"ws://127.0.0.1:{port}/sensors", additional_headers=headers) as ws:
                    clock = json.loads(await asyncio.wait_for(ws.recv(), 2))
                    device = time.monotonic_ns()//1000
                    await ws.send(json.dumps({"type": "clock", "id": clock["id"], "boot_id": "spider-boot",
                                              "received_monotonic_us": device, "served_monotonic_us": device}))
                    async def telemetry(seq, session="0123456789abcdef"):
                        data = packet(time.monotonic_ns()//1000, seq)
                        data["spider_motion"] = {"enabled": True, "uart_ready": True,
                            "joint_pose_available": True, "session": session, "last_accepted_sequence": 0}
                        await ws.send(json.dumps(data))
                        return await asyncio.to_thread(readings.get, True, 2)

                    await telemetry(1)
                    with self.assertRaisesRegex(ValueError, "robot_identity_required"):
                        request(service, COMMAND, {"robot_id": "rover", "mode": "walk", "action": "forward"})
                    intent = {"robot_id": "spider", "mode": "pose", "action": "lie"}
                    self.assertEqual(request(service, COMMAND, intent)["delivery"], "queued")
                    # A newly observed session must drop the previous session's command.
                    await asyncio.sleep(.02)
                    await telemetry(2, "fedcba9876543210")
                    with self.assertRaises(asyncio.TimeoutError):
                        await asyncio.wait_for(ws.recv(), .05)
                    await telemetry(3, "fedcba9876543210")
                    self.assertEqual(request(service, COMMAND, intent)["delivery"], "queued")
                    await asyncio.sleep(.02)
                    newest = await telemetry(4, "fedcba9876543210")
                    cmd = json.loads(await asyncio.wait_for(ws.recv(), 2))
                    self.assertEqual((cmd["mode"], cmd["action"], cmd["session"]), ("pose", "lie", "fedcba9876543210"))
                    t = newest["capture_monotonic_us"]
                    canonical = f"spider_command.v1\nfedcba9876543210\nspider-camera\n1\npose\nlie\n{t}\n{t+400000}"
                    self.assertEqual(cmd["signature"], hmac.new(SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest())
            finally:
                await asyncio.to_thread(receiver.close)
