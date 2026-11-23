"""Configuration storage must not take custody of raw receiver state.

Barriers wrap real model reads, fsync, reloads, and numeric scale matching.
The service uses temporary files and fixture cameras; no live receiver starts.
"""
import copy
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lidar_fusion
from lidar_fusion import LidarFusion, zone_points
from live_camera import LiveCamera
from rear_transport_preview import publish_sensor
from sensor_rig_registry import RigSampleFeed, configured_sensors
from vision_config import DEFAULT_CONFIG, atomic_json
from vision_service import VisionService
from test_lidar_database_isolation import sensor_packet, seed_raw, assert_raw, next_packet
from test_live_camera import Camera, frame
from test_vision_service import BackendFactory, FakeDetector, calibration


class Work:
    def __init__(self, fn, name):
        self.started = threading.Event()
        self.done = threading.Event()
        self.result = self.error = None

        def run():
            self.started.set()
            try:
                self.result = fn()
            except BaseException as error:
                self.error = error
            finally:
                self.done.set()

        self.thread = threading.Thread(target=run, name=name, daemon=True)
        self.thread.start()

    def value(self, timeout=1):
        if not self.done.wait(timeout):
            raise AssertionError("raw/configuration operation remained blocked")
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise AssertionError("completed operation did not terminate")
        if self.error is not None:
            raise self.error
        return self.result


class Barrier:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def wait(self):
        self.entered.set()
        if not self.release.wait(5):
            raise AssertionError("test did not release actual I/O boundary")


def custom_row(name="fixture.extra", value=7):
    return dict(id=name, type="fixture", role="aux", rate_hz=5,
                source=dict(kind="constant", value=value))


class RegistryConfigurationIsolationTests(unittest.TestCase):
    def make_service(self, directory):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["camera"]["url"] = "fixture://no-network"
        config["calibration"] = calibration()
        camera = Camera()
        service = VisionService(config, directory, camera=camera, detector=FakeDetector(),
                                backend_factory=BackendFactory())
        atomic_json(Path(directory) / "robot-model.json", {
            "schema_version": 1, "model_id": "fixture-model", "revision": 1,
            "camera": {"T_robot_camera": np.eye(4).tolist()},
        })
        return service, camera

    def raw_publication(self, service, camera, packet, barrier):
        ingress = SimpleNamespace(service=service, stop=threading.Event())
        service._live_camera = LiveCamera(camera, service.live_frames,
                                          service._raw_live_metadata).start()
        captured = frame(71)
        rear = None
        try:
            with service.live_frames.subscribe() as inbox:
                rear = Work(lambda: publish_sensor(ingress, packet, time.monotonic()),
                            "raw-rear-publication")
                rear.value()
                metadata, jpeg = next_packet(inbox, "rear", packet["frame_id"])
                self.assertEqual(jpeg, b"")
                self.assertEqual(metadata["sensor_packet"], packet)
                assert_raw(self, metadata["lidar_fusion"], packet)
                camera.offer(captured)
                metadata, jpeg = next_packet(inbox, "front", captured.metadata["frame_id"])
                self.assertEqual(jpeg, captured.jpeg_bytes)
                assert_raw(self, metadata["lidar"], packet)
                self.assertFalse(barrier.release.is_set())
        finally:
            # Release before joining real workers, including the old-code failure.
            if rear is not None and not rear.done.is_set():
                barrier.release.set()
                rear.thread.join(3)

    def test_model_read_does_not_block_raw_front_and_rear_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            service, camera = self.make_service(directory)
            fusion = service.lidar
            model = Path(directory) / "robot-model.json"
            packet = sensor_packet(11)
            seed_raw(fusion, packet)
            barrier = Barrier()
            original = Path.read_text

            def read(path, *args, **kwargs):
                if path == model:
                    barrier.wait()
                return original(path, *args, **kwargs)

            worker = None
            try:
                with patch.object(Path, "read_text", read):
                    worker = Work(lambda: RigSampleFeed(service).collect(), "registry-model-read")
                    self.assertTrue(barrier.entered.wait(1))
                    self.raw_publication(service, camera, packet, barrier)
                    barrier.release.set()
                    self.assertEqual(worker.value(3), [])
                self.assertTrue(fusion.config["sensors"])
            finally:
                barrier.release.set()
                if worker is not None:
                    worker.thread.join(3)
                service.close()

    def test_registry_fsync_keeps_raw_state_available_and_serializes_configure(self):
        with tempfile.TemporaryDirectory() as directory:
            service, camera = self.make_service(directory)
            fusion = service.lidar
            barrier = Barrier()
            original = os.fsync
            configured = copy.deepcopy(fusion.config)
            configured["sensors"] = [custom_row(value=19)]
            registry = writer = state = None

            def fsync(fd):
                if (threading.current_thread().name == "registry-fsync" and
                        stat.S_ISDIR(os.fstat(fd).st_mode)):
                    barrier.wait()
                return original(fd)

            try:
                with patch("vision_config.os.fsync", fsync):
                    registry = Work(lambda: configured_sensors(service), "registry-fsync")
                    self.assertTrue(barrier.entered.wait(1))
                    writer = Work(lambda: fusion.configure(configured), "configure-after-registry")
                    self.assertTrue(writer.started.wait(1))
                    self.assertFalse(writer.done.wait(.1), "configuration writers overlapped")
                    state = Work(fusion.status, "raw-status-during-registry-fsync")
                    self.assertEqual(state.value()["configuration"]["revision"], 1)
                    self.assertFalse(barrier.release.is_set())
                    barrier.release.set()
                    self.assertTrue(registry.value(3))
                    status = writer.value(3)
                    self.assertEqual(status["configuration"]["revision"], 2)
                disk = json.loads(fusion.path.read_text())
                self.assertEqual(disk, fusion.config)
                self.assertEqual(disk["sensors"], [custom_row(value=19)])
            finally:
                barrier.release.set()
                for work in (registry, writer, state):
                    if work is not None:
                        work.thread.join(3)
                service.close()

    def test_configure_fsync_keeps_raw_state_available_and_registry_uses_new_config(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _ = self.make_service(directory)
            fusion = service.lidar
            original_config = copy.deepcopy(fusion.config)
            configured = copy.deepcopy(original_config)
            configured["lidar"]["front"]["fov_horizontal_deg"] = 51.
            configured["sensors"] = [custom_row(value=23)]
            barrier = Barrier()
            original = os.fsync
            writer = registry = state = None

            def fsync(fd):
                if (threading.current_thread().name == "configure-fsync" and
                        stat.S_ISREG(os.fstat(fd).st_mode)):
                    barrier.wait()
                return original(fd)

            try:
                with patch("vision_config.os.fsync", fsync):
                    writer = Work(lambda: fusion.configure(configured), "configure-fsync")
                    self.assertTrue(barrier.entered.wait(1))
                    registry = Work(lambda: configured_sensors(service), "registry-after-configure")
                    self.assertTrue(registry.started.wait(1))
                    self.assertFalse(registry.done.wait(.1), "registry bypassed configuration custody")
                    state = Work(fusion.status, "raw-status-during-configure-fsync")
                    self.assertEqual(state.value()["configuration"], original_config)
                    self.assertFalse(barrier.release.is_set())
                    barrier.release.set()
                    writer.value(3)
                    rows = registry.value(3)
                self.assertIn(custom_row(value=23), rows)
                self.assertEqual(fusion.config["revision"], 2)
                self.assertEqual(fusion.config["lidar"]["front"]["fov_horizontal_deg"], 51.)
                self.assertEqual(json.loads(fusion.path.read_text()), fusion.config)
            finally:
                barrier.release.set()
                for work in (writer, registry, state):
                    if work is not None:
                        work.thread.join(3)
                service.close()

    def test_process_reload_cannot_overwrite_a_completed_configure(self):
        with tempfile.TemporaryDirectory() as directory:
            service = SimpleNamespace(state_dir=Path(directory), atlas=None, backend=None,
                                      map_id="fixture-map", camera_rig=SimpleNamespace(config={"rear": {}}))
            fusion = LidarFusion(service)
            old_disk = copy.deepcopy(fusion.config)
            old_disk["revision"] = 7
            old_disk["lidar"]["front"]["fov_horizontal_deg"] = 47.
            atomic_json(fusion.path, old_disk)
            configured = copy.deepcopy(fusion.config)
            configured["lidar"]["front"]["fov_horizontal_deg"] = 53.
            barrier = Barrier()
            original = Path.read_text
            reader = writer = None

            def read(path, *args, **kwargs):
                value = original(path, *args, **kwargs)
                if path == fusion.path and threading.current_thread().name == "process-reload":
                    barrier.wait()  # Hold the actual already-read old bytes.
                return value

            try:
                with patch.object(Path, "read_text", read):
                    reader = Work(lambda: fusion.process(sensor_packet(12), [1., 1.01],
                                                         service.map_id), "process-reload")
                    self.assertTrue(barrier.entered.wait(1))
                    writer = Work(lambda: fusion.configure(configured), "configure-during-reload")
                    self.assertTrue(writer.started.wait(1))
                    self.assertFalse(writer.done.wait(.1), "configure raced a stale disk reload")
                    barrier.release.set()
                    reader.value(3)
                    writer.value(3)
                self.assertEqual(fusion.config["revision"], 8)
                self.assertEqual(fusion.config["lidar"]["front"]["fov_horizontal_deg"], 53.)
                self.assertEqual(json.loads(fusion.path.read_text()), fusion.config)
            finally:
                barrier.release.set()
                for work in (reader, writer):
                    if work is not None:
                        work.thread.join(3)
                fusion.close()

    def test_hot_reload_preserves_native_projection_custom_overrides_and_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _ = self.make_service(directory)
            fusion = service.lidar
            try:
                configured_sensors(service)
                updated = copy.deepcopy(fusion.config)
                updated["revision"] = 9
                updated["lidar"]["front"]["T_front_camera_sensor_m"][0][3] = .125
                override = custom_row("camera.front", value=31)
                disabled = dict(id="tof.front", type="tof", role="front", enabled=False)
                updated["sensors"] = [override, disabled, custom_row(value=29)]
                atomic_json(fusion.path, updated)
                rows = configured_sensors(service)
                by_id = {row["id"]: row for row in rows}
                self.assertEqual(by_id["camera.front"], override)
                self.assertNotIn("tof.front", by_id)
                self.assertEqual(by_id["fixture.extra"], custom_row(value=29))
                self.assertEqual(by_id["lidar.front"]["T_body_sensor"][0][3], .125)
                self.assertEqual(fusion.config["revision"], 9)
                self.assertEqual(json.loads(fusion.path.read_text()), fusion.config)
                rows[0]["role"] = "caller-mutated"
                self.assertNotEqual(fusion.config["sensors"][0]["role"], "caller-mutated")
                # Actual packet processing sees the same hot-reloaded mounts/revision.
                fusion.survey_landmarks = None
                fusion.service.backend = None
                fusion.process(sensor_packet(13), [1., 1.01], service.map_id)
                self.assertEqual(fusion.latest["rig_revision"], 9)
                self.assertEqual(fusion.latest["sensors"][0]["mount"]["T_front_camera_sensor_m"][0][3], .125)
            finally:
                service.close()

    def test_failed_durable_writes_do_not_publish_partial_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _ = self.make_service(directory)
            fusion = service.lidar
            try:
                before = copy.deepcopy(fusion.config)
                with patch("vision_config.atomic_json", side_effect=OSError("fixture registry write")):
                    with self.assertRaisesRegex(OSError, "fixture registry write"):
                        configured_sensors(service)
                self.assertEqual(fusion.config, before)
                modified = copy.deepcopy(before)
                modified["revision"] = 5
                with patch("lidar_fusion.atomic_json", side_effect=OSError("fixture configure write")):
                    with self.assertRaisesRegex(OSError, "fixture configure write"):
                        fusion.configure(modified)
                self.assertEqual(fusion.config, before)
                self.assertTrue(configured_sensors(service))
                self.assertEqual(json.loads(fusion.path.read_text()), fusion.config)
            finally:
                service.close()

    def test_inflight_scan_keeps_revision_and_cannot_restore_cleared_scale(self):
        for native_birth in (False, True):
            with self.subTest(native_birth=native_birth), tempfile.TemporaryDirectory() as directory:
                identity = np.eye(4)
                commits, points = [], []
                backend = SimpleNamespace(
                    _lock=threading.RLock(), _map_id="fixture-map", _last_loop_id=4,
                    _native_birth=native_birth,
                    _slam=SimpleNamespace(map=SimpleNamespace(
                        update_lock=threading.RLock(), get_points=lambda: points)),
                    rig_resolve=lambda anchor: identity,
                    rig_anchor=lambda pose: {"keyframe_id": "kf-1", "T_keyframe_camera": identity.tolist()},
                    _read_entities=lambda: [], _diff=lambda entities: [],
                )
                service = SimpleNamespace(
                    state_dir=Path(directory), atlas=None, backend=backend,
                    map_id="fixture-map", revision=7, _pipeline_lock=threading.RLock(),
                    camera_rig=SimpleNamespace(config={"rear": {"max_pose_gap_s": 2.}}),
                    metric=SimpleNamespace(attachment_status=lambda map_id: {
                        "availability": "available", "scale_m_per_map_unit": 1.}),
                    _commit_changes=lambda changes, origin: commits.append((changes, origin)),
                )
                fusion = LidarFusion(service)
                packet = sensor_packet(14)
                for raw in packet["lidar"]["sensors"]:
                    mount = fusion.config["lidar"][raw["id"]]
                    transform = np.asarray(mount["T_front_camera_sensor_m"])
                    for point in zone_points(raw, mount, packet["capture_monotonic_us"]):
                        xyz = transform[:3, :3] @ point["xyz_sensor_m"] + transform[:3, 3]
                        class Point:
                            def __init__(self, value): self.value = value
                            def is_bad(self): return False
                            def pt(self): return self.value
                            def update_position(self, value): self.value = value
                        points.append(Point(xyz))
                fusion.visual.extend([(9., {}, service.map_id), (11., {}, service.map_id)])
                barrier = Barrier()
                actual_matches = lidar_fusion.scale_matches
                matched = []
                worker = None

                def match(*args, **kwargs):
                    barrier.wait()
                    result = actual_matches(*args, **kwargs)
                    matched.extend(result)
                    return result

                try:
                    with patch("lidar_fusion.scale_matches", match):
                        worker = Work(lambda: fusion.process(packet, [9.99, 10.01], service.map_id),
                                      "scan-before-configure")
                        self.assertTrue(barrier.entered.wait(1))
                        updated = copy.deepcopy(fusion.config)
                        updated["lidar"]["front"]["fov_horizontal_deg"] = 59.
                        fusion.configure(updated)
                        self.assertIsNone(fusion.scale)
                        barrier.release.set()
                        worker.value(3)
                    self.assertGreaterEqual(len(matched), 6, "actual scale-fit branch was not exercised")
                    self.assertIsNone(fusion.scale, "old scan restored scale after configure reset it")
                    self.assertEqual(fusion.config["revision"], 2)
                    self.assertEqual(fusion.latest["rig_revision"], 1)
                    self.assertTrue(fusion.latest["mapping_available"])
                    self.assertEqual(fusion.latest["scale"]["map_id"], service.map_id)
                    if not native_birth:
                        self.assertEqual(fusion.latest["scale"]["rig_revision"], 1)
                    records = [json.loads(row[0]) for row in fusion.db.execute("SELECT data FROM voxels")]
                    self.assertEqual(len(records), 32)
                    self.assertEqual({row["rig_revision"] for row in records}, {1})
                    ledger = [change["data"] for changes, origin in commits
                              if origin == "lidar_observations" for change in changes]
                    self.assertEqual([row["rig_revision"] for row in ledger], [1])
                finally:
                    barrier.release.set()
                    if worker is not None:
                        worker.thread.join(3)
                    fusion.close()


if __name__ == "__main__":
    unittest.main()
