"""The local raw status route must not wait for mapping or publishers.

Actual VisionService, CameraSource, RearSensorIngress and Unix HTTP dispatch are
used. No camera, native SLAM engine or network service is started.
"""
import copy
import http.client
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from camera_source import CameraSource
from live_camera import LiveCamera
from vision_config import DEFAULT_CONFIG
from vision_service import VisionService, LocalServer, handler_for
from test_vision_service import BackendFactory, FakeDetector, calibration


class Work:
    def __init__(self, function):
        self.done = threading.Event()
        self.result = None
        self.error = None

        def run():
            try:
                self.result = function()
            except BaseException as error:
                self.error = error
            finally:
                self.done.set()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def value(self, timeout=1):
        if not self.done.wait(timeout):
            raise AssertionError("raw receiver query waited for unrelated work")
        if self.error is not None:
            raise self.error
        return self.result


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=1)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


@contextmanager
def service_fixture():
    with tempfile.TemporaryDirectory() as directory:
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["camera"].update(url="fixture://no-network", capture_transport="websocket_push")
        config["calibration"] = calibration()
        camera = CameraSource(config["camera"])
        service = VisionService(config, directory, camera=camera, detector=FakeDetector(),
                                backend_factory=BackendFactory())
        rig = service.camera_rig
        rig.camera = CameraSource(config["camera"])
        service._live_camera = LiveCamera(camera, service.live_frames, service._raw_live_metadata)
        rig.publisher = LiveCamera(rig.camera, service.rear_live_frames, rig._raw_metadata)
        now = time.monotonic()
        for source in (camera, rig.camera):
            with source._condition:
                source._connected = True
                source._last_decode = now
                source._received = 11
                source._connections = 1
                source._epoch = "fixture-camera"
                source._capture_clock_known = True
        sensors = rig.sensors
        sensors.config = {"return_path_cs5_trial": True}
        sensors.last_wire_tick = now
        sensors.last_wire_boot = "boot-current"
        sensors.latest = {
            "packet": {"schema": "biome.sensor_tick.v1", "receiver_id": "rear-fixture",
                       "camera_id": "rear", "boot_id": "boot-current",
                       "frame_id": "boot-current:1000000", "capture_monotonic_us": 1000000,
                       "lidar": {"sensors": [
                           {"id": name, "ready": True, "valid": True, "resolution": 64,
                            "sequence": 0, "observed_monotonic_us": 999000,
                            "distance_mm": list(range(64)), "target_status": [5]*64}
                           for name in ("front", "rear")]}},
            "received": now, "interval": [now-.01, now],
        }
        sensors.return_path_cs5_attempts = 2
        sensors.return_path_cs5_last = {
            role: {"status": "applied", "requested_tos": 160, "readback_tos": 160,
                   "peer_port": 1234+i, "local_port": 9878}
            for i, role in enumerate(("camera", "sensors"))
        }
        try:
            yield service
        finally:
            service.close()


class RawReceiverStatusTests(unittest.TestCase):
    def assert_raw(self, service, value):
        self.assertEqual(value["schema"], "biome.vision.receiver-status.v1")
        self.assertEqual(value["state"], "running")
        self.assertEqual(value["map_id"], service.map_id)
        self.assertEqual(value["camera_rig"]["map_id"], service.map_id)
        for camera in (value["camera"], value["camera_rig"]["camera"]):
            self.assertEqual(camera["state"], "connected")
            self.assertIsNone(camera["error"])
            self.assertEqual(camera["frames_decoded"], 11)
            self.assertLess(camera["last_decode_age_s"], 2)
        sensors = value["camera_rig"]["sensor_push"]
        self.assertEqual(sensors["latest"], service.camera_rig.sensors.latest)
        self.assertEqual(sensors["return_path_cs5_trial"]["last"],
                         service.camera_rig.sensors.return_path_cs5_last)
        self.assertFalse(value["tracking_health_claimed"])
        self.assertLessEqual(value["observation_started_monotonic_s"],
                             value["observation_finished_monotonic_s"])

    def test_mapping_locks_do_not_block_raw_query(self):
        with service_fixture() as service:
            with ExitStack() as locks:
                for lock in (service.lidar.lock, service.lidar.db_lock,
                             service.atlas.lock, service.store._checkpoint_lock,
                             service.store._lock, service._pipeline_lock):
                    locks.enter_context(lock)
                worker = Work(lambda: service.query("robot.vision.status", {"receiver_only": True}))
                try:
                    self.assert_raw(service, worker.value())
                    worker.thread.join(1)
                    self.assertFalse(worker.thread.is_alive())
                finally:
                    locks.close()
                    worker.thread.join(3)

    def test_publisher_locks_do_not_block_real_unix_rpc(self):
        with service_fixture() as service:
            path = str(service.state_dir / "receiver-test.sock")
            server = LocalServer(path, handler_for(service))
            serving = threading.Thread(target=server.serve_forever, daemon=True)
            serving.start()

            def request():
                connection = UnixConnection(path)
                try:
                    connection.request("POST", "/v1/query", json.dumps({
                        "operation": "robot.vision.status", "inputs": {"receiver_only": True}}),
                        {"Content-Type": "application/json"})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    return json.loads(response.read())
                finally:
                    connection.close()

            worker = None
            try:
                with ExitStack() as locks:
                    locks.enter_context(service._live_camera._lock)
                    locks.enter_context(service.camera_rig.publisher._lock)
                    worker = Work(request)
                    try:
                        self.assert_raw(service, worker.value())
                    finally:
                        locks.close()
                        worker.thread.join(3)
            finally:
                server.shutdown()
                serving.join(3)
                server.server_close()

    def test_raw_error_staleness_identity_and_false_values_are_not_normalized(self):
        with service_fixture() as service:
            rig = service.camera_rig
            service.camera._error = "front_decode_failed"
            service.camera._last_decode = time.monotonic()-10
            rig.camera._connected = False
            rig.camera._error = "rear_disconnected"
            rig.sensors.error = "sensor_worker_failed"
            rig.sensors.last_wire_boot = "different-boot"
            rig.sensors.latest["packet"]["boot_id"] = "different-boot"
            rig.sensors.latest["packet"]["lidar"]["sensors"][0]["valid"] = False
            rig.sensors.return_path_cs5_last["camera"]["readback_tos"] = 0
            rig.error = "awaiting_synchronized_front_pose_or_shared_map_relocalization"
            value = service.query("robot.vision.status", {"receiver_only": True})
            self.assertEqual(value["camera"]["error"], "front_decode_failed")
            self.assertGreater(value["camera"]["last_decode_age_s"], 9)
            self.assertEqual(value["camera_rig"]["camera"]["state"], "disconnected")
            self.assertEqual(value["camera_rig"]["camera"]["error"], "rear_disconnected")
            sensors = value["camera_rig"]["sensor_push"]
            self.assertEqual(sensors["error"], "sensor_worker_failed")
            self.assertEqual(sensors["last_wire_boot"], "different-boot")
            self.assertEqual(sensors["latest"], rig.sensors.latest)
            self.assertIs(sensors["latest"]["packet"]["lidar"]["sensors"][0]["valid"], False)
            self.assertEqual(sensors["return_path_cs5_trial"]["last"]["camera"]["readback_tos"], 0)
            self.assertEqual(value["camera_rig"]["error"], rig.error)
            self.assertIs(value["tracking_health_claimed"], False)

    def test_missing_receivers_and_stopping_remain_explicit(self):
        with service_fixture() as service:
            camera, rig = service.camera, service.camera_rig
            try:
                service.camera = service.camera_rig = None
                service._stop.set()
                value = service.query("robot.vision.status", {"receiver_only": True})
                self.assertIsNone(value["camera"])
                self.assertIsNone(value["camera_rig"])
                self.assertEqual(value["state"], "stopping")
            finally:
                service.camera, service.camera_rig = camera, rig

    def test_legacy_dispatch_and_provider_only_are_unchanged(self):
        with service_fixture() as service:
            with patch("vision_service.dispatch", return_value={"legacy": True}) as dispatch:
                for inputs in ({}, {"receiver_only": False}, {"receiver_only": 1},
                               {"receiver_only": True, "extra": True}):
                    self.assertEqual(service.query("robot.vision.status", inputs), {"legacy": True})
                    self.assertEqual(dispatch.call_args.args[2], inputs)
                count = dispatch.call_count
                value = service.query("robot.vision.status", {"provider_only": True})
                self.assertEqual(value["state"], "running")
                self.assertEqual(value["map_id"], service.map_id)
                self.assertNotIn("camera", value)
                self.assertEqual(dispatch.call_count, count)


if __name__ == "__main__":
    unittest.main()
