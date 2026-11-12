"""AHRS service boundaries with the real cache and injected firmware responses."""
import contextlib
import copy
from datetime import datetime, timezone
import io
import json
import math
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ahrs_source import AHRSTelemetrySource
from camera_telemetry import CameraHTTPError
from vision_config import DEFAULT_CONFIG
from vision_service import CAPABILITIES, VisionService
from test_vision_service import FakeCamera, FakeDetector, BackendFactory, calibration, identity_pose
import test_ahrs_source as wire


class AHRSServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="biome-ahrs-integration-")
        self.root = Path(self.tmp.name)
        self.services = []
        self.sequence = 0

    def tearDown(self):
        for service in reversed(self.services):
            if not service.store._closed:
                try:
                    service.close()
                finally:
                    service.store.close()
        self.tmp.cleanup()

    def make_service(self, *, enabled=True, history_capacity=256):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["camera"].update(url="fixture://no-network", source_id="synthetic-ahrs",
                                expected_width=32, expected_height=24)
        config["calibration"] = calibration()
        config["ahrs"].update(enabled=enabled, history_capacity=history_capacity)
        self.clock = [100.0]
        self.client = wire.Client()
        self.source = AHRSTelemetrySource(
            config["camera"], config["ahrs"], client=self.client,
            monotonic=lambda: self.clock[0], utc_clock=lambda: "synthetic-receipt-utc")
        self.factory = BackendFactory()
        service = VisionService(
            config, self.root / ("service-" + str(len(self.services))),
            camera=FakeCamera(), detector=FakeDetector(),
            backend_factory=self.factory, ahrs_source=self.source)
        self.services.append(service)
        self.sample_step = 0
        return service

    def poll(self, tick=100.0, angle=0.0, *, modify=None):
        self.clock[0] = tick
        value = wire.telemetry(
            uptime_ms=1000 + round((tick - 100.0) * 1000), sample_step=self.sample_step)
        self.sample_step += 1
        angle_rad = math.radians(angle)
        value["ahrs"]["attitude"].update(
            quaternion_wxyz=[math.cos(angle_rad / 2), 0.0, 0.0, math.sin(angle_rad / 2)],
            yaw_deg=angle)
        if modify:
            modify(value)
        self.client.values.append(value)
        self.assertTrue(self.source.poll_once())
        return value

    def frame(self, service, tick, name="frame"):
        self.sequence += 1
        return SimpleNamespace(
            image=np.full((24, 32, 3), 40, dtype=np.uint8),
            sequence=self.sequence, decoded_monotonic=tick,
            metadata={
                "frame_id": service.session_id + ":" + name + ":" + str(self.sequence),
                "sequence": self.sequence, "stream_epoch": "synthetic-stream",
                "width": 32, "height": 24,
                "receive_decode_time": datetime.now(timezone.utc).isoformat(),
                "capture_time": None, "source_timestamp_quality": "receiver_only",
            })

    def process(self, service, tick, name="frame"):
        self.clock[0] = max(self.clock[0], tick)
        with service._pipeline_lock:
            return service.process_frame(self.frame(service, tick, name))

    def test_disabled_source_never_polls_and_preserves_visual_pose(self):
        service = self.make_service(enabled=False)
        self.assertFalse(self.source.poll_once())
        snapshot = self.process(service, 100.0)
        evidence = snapshot["spatial_evidence"]["ahrs"]
        self.assertFalse(evidence["enabled"])
        self.assertFalse(evidence["available"])
        self.assertEqual(snapshot["localization"]["T_map_camera"], identity_pose())
        self.assertTrue(snapshot["localization"]["valid"])
        self.assertEqual(service.query("robot.ahrs.snapshot")["availability"], "unavailable")
        service.query("robot.ahrs.status")
        self.assertEqual(self.client.calls, 0)
        self.assertEqual(self.client.posts, 0)

    def test_observation_binds_compact_sensor_evidence_without_network_reads(self):
        service = self.make_service()
        raw = self.poll()
        snapshot = self.process(service, 100.05)
        evidence = snapshot["spatial_evidence"]["ahrs"]
        self.assertEqual(evidence["frame_id"], snapshot["frame_id"])
        self.assertEqual(evidence["map_id"], snapshot["map_id"])
        self.assertEqual(evidence["session_id"], service.session_id)
        self.assertEqual(evidence["calibration_id"], service.calibration["calibration_id"])
        self.assertTrue(evidence["sensors"]["imu"]["valid"])
        self.assertNotIn("telemetry", evidence)
        self.assertFalse(evidence["camera_pose_corrected"])
        self.assertFalse(evidence["navigation_usable"])
        self.assertFalse(evidence["heading_available"])
        self.assertFalse(evidence["metric_scale_available"])
        self.assertFalse(evidence["timing"]["timing_qualified"])
        self.assertEqual(snapshot["localization"]["T_map_camera"], identity_pose())
        reply = service.query("robot.ahrs.snapshot")
        self.assertEqual(reply["snapshot"]["telemetry"], raw)
        service.query("robot.ahrs.status")
        service.query("robot.vision.status")
        service.query("robot.vision.metadata")
        self.assertEqual(self.client.calls, 1)
        self.assertEqual(self.client.posts, 0)

    def test_future_receipt_does_not_leak_into_an_older_camera_frame(self):
        service = self.make_service()
        self.poll(100.0, 0)
        self.poll(100.2, 45)
        self.clock[0] = 100.3
        earlier = self.process(service, 100.1, "earlier")
        later = self.process(service, 100.3, "later")
        first = earlier["spatial_evidence"]["ahrs"]
        second = later["spatial_evidence"]["ahrs"]
        self.assertEqual(first["timing"]["received_monotonic_s"], 100.0)
        self.assertEqual(second["timing"]["received_monotonic_s"], 100.2)
        self.assertTrue(second["sensor_rotation"]["available"])
        self.assertAlmostEqual(second["sensor_rotation"]["angle_deg"], 45.0)
        self.assertEqual(second["sensor_rotation"]["previous_frame_id"], earlier["frame_id"])
        self.assertEqual(self.client.calls, 2)
        self.assertEqual(later["localization"]["T_map_camera"], identity_pose())

    def test_old_firmware_failure_does_not_break_vision_or_radio_queries(self):
        service = self.make_service()
        self.client.values.append(CameraHTTPError("camera_http_error", http_status=404))
        self.assertFalse(self.source.poll_once())
        snapshot = self.process(service, 100.01)
        self.assertTrue(snapshot["localization"]["valid"])
        self.assertFalse(snapshot["spatial_evidence"]["ahrs"]["available"])
        self.assertEqual(service.query("robot.vision.status")["availability"], "available")
        self.assertIsInstance(service.query("robot.radio.status"), dict)
        self.assertFalse(self.source.status()["supported"])
        self.assertEqual(self.client.calls, 1)

    def test_malformed_one_sensor_keeps_other_sensor_evidence(self):
        service = self.make_service()
        self.poll(modify=lambda value: value["ahrs"]["mag"].update(field_uT=[1.0, 2.0]))
        snapshot = self.process(service, 100.03)
        sensors = snapshot["spatial_evidence"]["ahrs"]["sensors"]
        self.assertFalse(sensors["mag"]["valid"])
        self.assertTrue(sensors["imu"]["valid"])
        self.assertTrue(sensors["attitude"]["valid"])
        self.assertTrue(sensors["barometer"]["valid"])
        self.assertTrue(snapshot["localization"]["valid"])

    def test_stale_motion_does_not_disable_fresher_pressure_measurement(self):
        service = self.make_service()
        self.poll()
        snapshot = self.process(service, 100.7)
        sensors = snapshot["spatial_evidence"]["ahrs"]["sensors"]
        self.assertFalse(sensors["imu"]["valid"])
        self.assertFalse(sensors["attitude"]["valid"])
        self.assertTrue(sensors["barometer"]["valid"])
        self.assertFalse(snapshot["spatial_evidence"]["ahrs"]["sensor_rotation"]["available"])
        self.assertTrue(snapshot["localization"]["valid"])

    def test_failed_publication_never_accepts_motion_baseline(self):
        service = self.make_service()
        self.poll()
        self.process(service, 100.01)
        self.poll(100.2, 30)
        with patch.object(service.store, "publish_observation", side_effect=OSError("fixture")), \
             patch.object(service.ahrs, "record_pose", wraps=service.ahrs.record_pose) as accept:
            with self.assertRaises(OSError):
                self.process(service, 100.21, "failed")
            accept.assert_not_called()
        self.poll(100.4, 60)
        result = self.process(service, 100.41, "after-failure")
        self.assertFalse(result["spatial_evidence"]["ahrs"]["sensor_rotation"]["available"])
        self.poll(100.6, 90)
        recovered = self.process(service, 100.61, "recovered")
        self.assertAlmostEqual(recovered["spatial_evidence"]["ahrs"]["sensor_rotation"]["angle_deg"], 30.0)

    def test_failed_keyframe_pin_never_accepts_motion_baseline(self):
        service = self.make_service()
        self.poll()
        with patch.object(service.store, "pin_frame", side_effect=OSError("fixture")), \
             patch.object(service.ahrs, "record_pose", wraps=service.ahrs.record_pose) as accept:
            with self.assertRaises(OSError):
                self.process(service, 100.01)
            accept.assert_not_called()
        self.poll(100.2, 30)
        result = self.process(service, 100.21)
        self.assertFalse(result["spatial_evidence"]["ahrs"]["sensor_rotation"]["available"])

    def test_new_map_resets_motion_continuity(self):
        service = self.make_service()
        self.poll()
        first = self.process(service, 100.01)
        previous_session = service.session_id
        created = service.query("robot.map.create")
        service.query("robot.map.open", {"map_id": created["map_id"]})
        self.assertNotEqual(service.map_id, first["map_id"])
        self.assertNotEqual(service.session_id, previous_session)
        self.poll(100.2, 30)
        current = self.process(service, 100.21)
        evidence = current["spatial_evidence"]["ahrs"]
        self.assertEqual(evidence["session_id"], service.session_id)
        self.assertEqual(evidence["map_id"], service.map_id)
        self.assertFalse(evidence["sensor_rotation"]["available"])

    def test_queued_calibration_while_polling_disabled_is_not_completion(self):
        service = self.make_service(enabled=False)
        original = (service.map_id, service.session_id, service.calibration["calibration_id"])
        result = service.query("robot.ahrs.calibrate")
        receipt = result["calibration_command"]
        self.assertTrue(receipt["accepted"])
        self.assertEqual(receipt["status"], "queued")
        self.assertFalse(receipt["completed"])
        self.assertFalse(receipt["completion_correlated"])
        self.assertFalse(result["motion_commands"])
        self.assertEqual(self.client.posts, 1)
        self.assertEqual(self.client.calls, 0)
        self.assertEqual(self.source.status()["calibration_request"], receipt)
        self.assertEqual(original, (service.map_id, service.session_id, service.calibration["calibration_id"]))

    def test_unknown_calibration_outcome_is_preserved_without_retry(self):
        service = self.make_service()
        self.client.calibration_result = CameraHTTPError(
            "camera_http_transport_error", outcome_known=False)
        result = service.query("robot.ahrs.calibrate")
        self.assertEqual(result["availability"], "unavailable")
        self.assertFalse(result["outcome_known"])
        self.assertEqual(self.client.posts, 1)
        self.assertEqual(self.source.status()["calibration_request"]["status"], "unknown")
        self.assertFalse(self.source.status()["calibration_request"]["completed"])
        self.assertTrue(self.process(service, 100.01)["localization"]["valid"])

    def test_start_and_close_manage_ahrs_worker_independently(self):
        service = self.make_service()
        with patch.object(self.source, "start", return_value=self.source) as start, \
             patch.object(self.source, "close", wraps=self.source.close) as close, \
             patch.object(service, "_run", return_value=None):
            service.start()
            service.close()
        start.assert_called_once_with()
        close.assert_called_once_with()

    def test_raw_history_is_bounded_and_excluded_from_checkpoint_files(self):
        service = self.make_service(history_capacity=2)
        marker = "unique-raw-AHRS-fixture-marker"
        for tick, angle in [(100.0, 0), (100.2, 10), (100.4, 20)]:
            self.poll(tick, angle, modify=lambda value: value.update(debug_fixture=marker))
            self.process(service, tick + 0.01)
        self.assertEqual(self.source.status()["history_size"], 2)
        self.assertEqual(service.query("robot.ahrs.snapshot")["snapshot"]["telemetry"]["debug_fixture"], marker)
        saved = service.query("robot.map.checkpoint")
        self.assertEqual(saved["backend_artifact"]["availability"], "available")
        for path in service.state_dir.rglob("*"):
            if path.is_file():
                self.assertNotIn(marker.encode(), path.read_bytes(), str(path))
        self.assertFalse((service.state_dir / "ahrs").exists())


class AHRSProviderTests(unittest.TestCase):
    def test_additive_contracts_and_guidance_preserve_mutation_distinction(self):
        import biome_provider as provider
        expected = {"robot.ahrs.status", "robot.ahrs.snapshot", "robot.ahrs.calibrate"}
        self.assertEqual(len(CAPABILITIES), 32)
        self.assertEqual(set(CAPABILITIES), {spec["name"] for spec in provider.CONTRACTS.values()})
        self.assertTrue(expected.issubset(CAPABILITIES))
        for name in expected:
            self.assertEqual(provider.CONTRACTS[name + ":1"]["inputs"], {})
        self.assertIn("robot.ahrs.calibrate:1", provider.MUTATING_CONTRACTS)
        self.assertNotIn("robot.ahrs.status:1", provider.MUTATING_CONTRACTS)
        self.assertNotIn("robot.ahrs.snapshot:1", provider.MUTATING_CONTRACTS)
        guide = provider.offer(
            {"provider": "fixture", "resource": "fixture", "slots": 1},
            "robot.ahrs.calibrate:1", "ready")["usage"]
        self.assertIn("Queued means accepted, not completed", guide["read_result"])
        self.assertIn("do not automatically retry", guide["read_result"])

    def test_lost_provider_reply_to_calibration_preserves_unknown_effect(self):
        import biome_provider as provider
        for code in ("service_unavailable", "invalid_response", "response_too_large"):
            with self.subTest(code=code):
                output = io.StringIO()
                with patch.object(provider, "load_config", return_value={}), \
                     patch.object(provider, "Remote"), \
                     patch.object(provider, "execute", side_effect=provider.ServiceError(code, "fixture")), \
                     patch.object(sys, "stdin", io.StringIO('{"inputs":{}}')), \
                     contextlib.redirect_stdout(output):
                    exit_code = provider.main(["execute", "--contract", "robot.ahrs.calibrate:1"])
                self.assertEqual(exit_code, 0)
                error = json.loads(output.getvalue())["result"]["error"]
                self.assertEqual(error["effect"], "unknown")
                self.assertIn("Inspect current state", error["next_action"])


if __name__ == "__main__":
    unittest.main()
