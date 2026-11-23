"""Storage stalls must not take custody of the raw LiDAR/camera state.

The barriers delegate to a real SQLite connection. They stop inside the
production transaction-exit or SELECT/fetchall path, not around a test-held
application lock. Subscriber assertions happen before releasing the barrier.
"""
import asyncio
import copy
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lidar_fusion import LidarFusion
from live_camera import LiveCamera
from rear_transport_preview import publish_sensor
from vision_config import DEFAULT_CONFIG
from vision_service import VisionService
from test_live_camera import Camera, frame, unpack
from test_vision_service import BackendFactory, FakeDetector, calibration


class Work:
    def __init__(self, fn):
        self.started = threading.Event()
        self.done = threading.Event()
        self.result = None
        self.error = None

        def run():
            self.started.set()
            try:
                self.result = fn()
            except BaseException as exc:
                self.error = exc
            finally:
                self.done.set()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def value(self, timeout=1):
        if not self.done.wait(timeout):
            raise AssertionError("production operation blocked behind SQLite")
        if self.error is not None:
            raise self.error
        return self.result


class DatabaseBarrier:
    """Only the selected SQLite boundary blocks; every SQL operation is real."""
    def __init__(self, connection, boundary):
        self.connection = connection
        self.boundary = boundary
        self.entered = threading.Event()
        self.release = threading.Event()
        self.select_entered = threading.Event()
        self.close_entered = threading.Event()
        self.in_transaction = False
        self.overlap = False

    def wait(self):
        self.entered.set()
        if not self.release.wait(5):
            raise AssertionError("test did not release database barrier")

    def __enter__(self):
        self.connection.__enter__()
        self.in_transaction = True
        return self

    def __exit__(self, *args):
        try:
            if self.boundary == "commit":
                self.wait()
            return self.connection.__exit__(*args)
        finally:
            self.in_transaction = False

    def execute(self, sql, args=()):
        is_select = sql.lstrip().upper().startswith("SELECT")
        if is_select:
            self.select_entered.set()
            self.overlap |= self.in_transaction
        cursor = self.connection.execute(sql, args)
        if is_select and self.boundary == "fetchall":
            owner = self

            class Cursor:
                def fetchall(self):
                    owner.wait()
                    return cursor.fetchall()

            return Cursor()
        return cursor

    def close(self):
        self.close_entered.set()
        self.connection.close()


def sensor_packet(sequence):
    capture = 1000000 + sequence * 100000
    return {
        "schema": "biome.sensor_tick.v1", "receiver_id": "rear-fixture",
        "boot_id": "boot-fixture", "camera_id": "rear",
        "frame_id": "boot-fixture:" + str(capture), "stream_sequence": sequence,
        "capture_clock": "esp_timer", "capture_monotonic_us": capture,
        "lidar": {"schema": "rover.lidar.v1", "worker_ready": True, "sensors": [
            {"id": name, "ready": True, "valid": True, "sequence": sequence,
             "resolution": 16, "observed_monotonic_us": capture - 1000,
             "distance_mm": [1000 + sequence] * 16, "target_status": [5] * 16,
             "errors": 0} for name in ("front", "rear")]},
    }


def seed_raw(fusion, packet):
    fusion.latest = {
        "schema": "rover.lidar-fusion/1", "packet_id": "previous-fused-packet",
        "received_monotonic": time.monotonic(), "mapping_available": True,
        "native_landmarks_refined": 7, "sensors": [],
    }
    now = time.monotonic()
    fusion.enqueue(packet, [now - .001, now + .001])


def assert_raw(test, metadata, packet):
    latest = metadata["latest"]
    test.assertEqual(latest["packet_id"], packet["frame_id"])
    test.assertEqual(latest["fusion_packet_id"], "previous-fused-packet")
    test.assertTrue(latest["mapping_available"])
    test.assertEqual(latest["native_landmarks_refined"], 7)
    test.assertEqual([row["raw"] for row in latest["sensors"]], packet["lidar"]["sensors"])
    test.assertTrue(all(row["mount"]["T_front_camera_sensor_m"] for row in latest["sensors"]))


def next_packet(inbox, camera, frame_id, timeout=1):
    deadline = time.monotonic() + timeout
    while True:
        metadata, jpeg = unpack(inbox.get(timeout=max(.001, deadline-time.monotonic())))
        if metadata.get("camera_id") == camera and metadata.get("frame_id") == frame_id:
            return metadata, jpeg
        if time.monotonic() >= deadline:
            raise AssertionError("expected raw publication did not arrive")


class LidarDatabaseIsolationTests(unittest.TestCase):
    def make_service(self, directory):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["camera"]["url"] = "fixture://no-network"
        config["calibration"] = calibration()
        camera = Camera()
        service = VisionService(config, directory, camera=camera, detector=FakeDetector(),
                                backend_factory=BackendFactory())
        return service, camera

    def test_blocked_query_preserves_rear_preview_and_complete_raw_grids(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _ = self.make_service(directory)
            fusion = service.lidar
            old = sensor_packet(1)
            latest = sensor_packet(2)
            seed_raw(fusion, old)
            barrier = DatabaseBarrier(fusion.db, "fetchall")
            fusion.db = barrier
            reader = Work(lambda: fusion.entities(service.map_id))
            try:
                self.assertTrue(barrier.entered.wait(1), "real SELECT did not reach fetchall")
                ingress = SimpleNamespace(service=service, _stop=threading.Event(),
                                          _preview_identity=None, _pending_preview=None,
                                          _preview_task=None)
                with service.live_frames.subscribe() as inbox:
                    async def deliver():
                        publish_sensor(ingress, latest, time.monotonic())
                        task = ingress._preview_task
                        try:
                            await asyncio.wait_for(asyncio.shield(task), 1)
                            metadata, jpeg = next_packet(inbox, "rear", latest["frame_id"])
                            self.assertEqual(jpeg, b"")
                            self.assertEqual(metadata["sensor_packet"], latest)
                            assert_raw(self, metadata["lidar"], latest)
                            assert_raw(self, metadata["lidar_fusion"], latest)
                            self.assertFalse(barrier.release.is_set())
                        finally:
                            # Also release before asyncio.run joins its executor
                            # when the unchanged source fails the delivery assertion.
                            barrier.release.set()
                            await asyncio.wait_for(asyncio.shield(task), 3)
                    asyncio.run(deliver())
                self.assertEqual(reader.value(), [])
            finally:
                barrier.release.set()
                reader.thread.join(3)
                service.close()

    def test_blocked_query_preserves_front_jpeg_and_fusion_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            service, camera = self.make_service(directory)
            fusion = service.lidar
            packet = sensor_packet(3)
            seed_raw(fusion, packet)
            barrier = DatabaseBarrier(fusion.db, "fetchall")
            fusion.db = barrier
            reader = Work(lambda: fusion.entities(service.map_id))
            try:
                self.assertTrue(barrier.entered.wait(1), "real SELECT did not reach fetchall")
                service._live_camera = LiveCamera(camera, service.live_frames,
                                                  service._raw_live_metadata).start()
                captured = frame(42)
                with service.live_frames.subscribe() as inbox:
                    camera.offer(captured)
                    metadata, jpeg = next_packet(inbox, "front", captured.metadata["frame_id"])
                    self.assertEqual(jpeg, captured.jpeg_bytes)
                    self.assertEqual(metadata["camera_delivery"], "independent_capture")
                    assert_raw(self, metadata["lidar"], packet)
                    self.assertFalse(barrier.release.is_set())
                self.assertEqual(fusion.for_camera({}, "unknown", {}), None)
            finally:
                barrier.release.set()
                reader.thread.join(3)
                service.close()
            self.assertEqual(reader.value(), [])

    def test_blocked_commit_allows_state_and_preserves_database_serialization(self):
        with tempfile.TemporaryDirectory() as directory:
            commits = []
            identity = np.eye(4)
            backend = SimpleNamespace(
                _lock=threading.RLock(), _map_id="map-captured", _last_loop_id=4,
                _slam=SimpleNamespace(map=SimpleNamespace(
                    update_lock=threading.RLock(), get_points=lambda: [])),
                rig_resolve=lambda anchor: identity,
                rig_anchor=lambda pose: {"keyframe_id": "kf-1", "T_keyframe_camera": identity.tolist()},
            )
            service = SimpleNamespace(
                state_dir=Path(directory), atlas=None, backend=backend,
                map_id="map-captured", revision=7, _pipeline_lock=threading.RLock(),
                metric=SimpleNamespace(attachment_status=lambda map_id: {
                    "availability": "available", "scale_m_per_map_unit": 1.}),
                _commit_changes=lambda changes, origin: commits.append((changes, origin)),
                _sonar_anchor=lambda map_id, key, revision: {"T_map_keyframe": identity.tolist()},
            )
            fusion = LidarFusion(service)
            initial, latest = sensor_packet(4), sensor_packet(5)
            seed_raw(fusion, initial)
            fusion.visual.extend([(9., {}, service.map_id), (11., {}, service.map_id)])
            barrier = DatabaseBarrier(fusion.db, "commit")
            fusion.db = barrier
            writer = Work(lambda: fusion.process(initial, [9.99, 10.01], service.map_id))
            reader = state = None
            try:
                self.assertTrue(barrier.entered.wait(1), "actual INSERT transaction did not reach commit")

                def update_state():
                    fusion.enqueue(latest, [10.09, 10.11])
                    updated = copy.deepcopy(fusion.config)
                    updated["revision"] = 3
                    fusion.configure(updated)
                    return fusion.status()

                state = Work(update_state)
                status = state.value()
                assert_raw(self, status, latest)
                self.assertEqual(status["configuration"]["revision"], 3)
                reader = Work(lambda: fusion.entities(service.map_id, revision=8))
                self.assertTrue(reader.started.wait(1))
                self.assertFalse(barrier.select_entered.wait(.1),
                                 "query used the same SQLite connection during its transaction")
                self.assertFalse(barrier.release.is_set())
                barrier.release.set()
                writer.value(3)
                entities = reader.value(3)
                self.assertEqual(len(entities), 32)
                self.assertFalse(barrier.overlap)
                rows = barrier.connection.execute(
                    "SELECT map_id,data FROM voxels ORDER BY voxel").fetchall()
                self.assertEqual(len(rows), 32)
                self.assertEqual({row[0] for row in rows}, {"map-captured"})
                records = [json.loads(row[1]) for row in rows]
                self.assertEqual({row["rig_revision"] for row in records}, {1})
                self.assertEqual({row["recorded_revision"] for row in records}, {8})
                self.assertEqual({row["sensor_id"] for row in records}, {"front", "rear"})
                self.assertEqual(fusion.entities(service.map_id, revision=7), [])
                self.assertEqual(fusion.entities("different-map"), [])
                self.assertEqual(fusion.latest["packet_id"], initial["frame_id"])
                self.assertEqual(fusion.raw_latest[0]["frame_id"], latest["frame_id"])
                self.assertEqual([row[1] for row in commits], ["lidar_observations"])
            finally:
                barrier.release.set()
                for work in (writer, reader, state):
                    if work is not None:
                        work.thread.join(3)
                fusion.close()

    def test_close_serializes_with_inflight_query_without_blocking_raw_state(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _ = self.make_service(directory)
            fusion = service.lidar
            packet = sensor_packet(6)
            seed_raw(fusion, packet)
            barrier = DatabaseBarrier(fusion.db, "fetchall")
            fusion.db = barrier
            reader = Work(lambda: fusion.entities(service.map_id))
            closer = status = None
            try:
                self.assertTrue(barrier.entered.wait(1))
                closer = Work(fusion.close)
                self.assertTrue(closer.started.wait(1))
                self.assertFalse(barrier.close_entered.wait(.1),
                                 "SQLite was closed before the active query finished")
                status = Work(fusion.status)
                assert_raw(self, status.value(), packet)
                self.assertFalse(barrier.release.is_set())
                barrier.release.set()
                self.assertEqual(reader.value(3), [])
                closer.value(3)
                self.assertTrue(barrier.close_entered.is_set())
            finally:
                barrier.release.set()
                for work in (reader, closer, status):
                    if work is not None:
                        work.thread.join(3)
                service.close()


if __name__ == "__main__":
    unittest.main()
