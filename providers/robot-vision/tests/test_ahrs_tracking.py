"""Causal, descriptive AHRS evidence; fixtures never contact the robot."""
import copy
import json
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ahrs_tracking import AHRSTracking
from camera_telemetry import CameraHTTPError


def quaternion(degrees):
    half = math.radians(degrees) / 2.0
    return [math.cos(half), 0.0, 0.0, math.sin(half)]


def frame(tick, **changes):
    result = {"frame_id": "frame-%s" % tick, "source_id": "camera-one",
              "stream_epoch": "stream-one", "calibration_id": "cal-one",
              "receive_decode_monotonic_s": tick}
    result.update(changes)
    return result


def localization(captured, **changes):
    result = {"valid": True, "map_id": "map-one",
              "frame_id": captured["frame_id"], "T_map_camera": [
                  [1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]}
    result.update(changes)
    return result


def envelope(tick, *, degrees=0.0, sample_count=None, observed_ms=None,
             receiver="camera-one", boot="boot-one", generation=1,
             origin=1000, altitude=100.0):
    observed_ms = int(round(tick * 1000)) if observed_ms is None else observed_ms
    sample_count = int(round(tick * 50)) if sample_count is None else sample_count
    payload = {
        "worker_ready": True, "paused": False,
        "imu": {"present": True, "valid": True, "frame": "mpu9250_native_xyz",
                "accel_m_s2": [0.0, 0.0, 9.80665], "gyro_rad_s": [0.0, 0.0, 0.5],
                "gyro_raw_rad_s": [0.01, 0.02, 0.53], "temp_c": 24.0,
                "gyro_bias_applied": True},
        "mag": {"present": True, "valid": True, "frame": "ak8963_native_xyz",
                "field_uT": [10.0, 20.0, 30.0],
                "calibration": "factory_sensitivity_only", "used_for_heading": False},
        "barometer": {"present": True, "valid": True, "pressure_pa": 101000.0,
                      "temp_c": 25.0, "pressure_altitude_m": altitude,
                      "sea_level_reference_pa": 101325,
                      "altitude_reference": "ISA_standard_pressure_not_AGL"},
        "attitude": {"valid": True, "quaternion_wxyz": quaternion(degrees),
                     "roll_deg": 0.0, "pitch_deg": 0.0, "yaw_deg": degrees,
                     "reference": "relative_yaw",
                     "frame": "MPU_axes_to_gravity_aligned_initial_yaw",
                     "generation": generation, "yaw_origin_uptime_ms": origin,
                     "reset_reason": "startup", "heading_absolute": False,
                     "mounting_alignment_applied": False},
        "calibration": {"status": "succeeded", "gyro_bias_valid": True,
                        "gyro_bias_rad_s": [0.01, 0.02, 0.03]},
    }
    sensors = {
        name: {"present": True, "valid": True, "reason": "available",
               "age_ms": 0.0, "age_upper_bound_ms": 0.0,
               "sample_count": sample_count, "observed_uptime_ms": observed_ms,
               "data": copy.deepcopy(payload[name])}
        for name in ("imu", "mag", "barometer", "attitude")
    }
    return {
        "enabled": True, "available": True, "valid": True,
        "receiver_id": receiver, "boot_id": boot, "sequence": 7,
        "received_monotonic": tick, "request_duration_s": 0.0,
        "received_at": "2026-11-10T01:00:00+00:00",
        "telemetry": {"ahrs": payload}, "sensors": sensors,
        "calibration": {"valid": True, "reason": "available",
                        "data": copy.deepcopy(payload["calibration"])},
    }


class FakeSource:
    def __init__(self):
        self.value = None
        self.started = self.closed = False
        self.calls = []
        self.calibration_calls = 0
        self.fail = None

    def start(self):
        if self.fail == "start":
            raise OSError("fixture")
        self.started = True

    def close(self):
        if self.fail == "close":
            raise OSError("fixture")
        self.closed = True

    def snapshot(self, at_monotonic=None):
        self.calls.append(at_monotonic)
        if self.fail == "snapshot":
            raise OSError("fixture")
        return copy.deepcopy(self.value)

    def status(self):
        if self.fail == "status":
            raise OSError("fixture")
        return {"state": "fixture", "network_used": False}

    def calibrate_gyro(self):
        self.calibration_calls += 1
        if self.fail == "calibrate_http":
            raise CameraHTTPError("camera_http_error", http_status=409,
                                  response={"status": "already_pending"})
        if self.fail == "calibrate":
            raise OSError("fixture")
        self.value = None
        return {"status": "queued", "calibration_completed": False}


class AHRSTrackingTests(unittest.TestCase):
    def setUp(self):
        self.source = FakeSource()
        self.tracking = AHRSTracking({"url": "fixture://no-network"},
                                     {"enabled": True}, source=self.source)
        self.tracking.set_context("map-one", "cal-one", "session-one")
        self.addCleanup(self.tracking.close)

    def observe(self, tick, *, accept=False, value=None, frame_changes=None,
                localization_changes=None, **options):
        self.source.value = envelope(tick, **options) if value is None else value
        captured = frame(tick, **(frame_changes or {}))
        visual = localization(captured, **(localization_changes or {}))
        result = self.tracking.evidence(captured, visual)
        if accept:
            self.assertTrue(self.tracking.record_pose(captured, visual))
        return result, captured, visual

    def test_known_rotation_and_norms_are_descriptive_without_visual_pose_changes(self):
        self.observe(100.0, accept=True)
        result, captured, visual = self.observe(100.2, degrees=90.0, accept=True)
        self.assertAlmostEqual(result["sensor_rotation"]["angle_deg"], 90.0)
        self.assertAlmostEqual(result["sensor_rotation"]["interval_s"], 0.2)
        self.assertEqual(result["sensor_rotation"]["previous_frame_id"], "frame-100.0")
        self.assertAlmostEqual(result["sensors"]["imu"]["accel_norm_m_s2"], 9.80665)
        self.assertAlmostEqual(result["sensors"]["imu"]["gyro_norm_rad_s"], 0.5)
        self.assertTrue(result["sensors"]["imu"]["acceleration_includes_gravity"])
        self.assertEqual(visual, localization(captured))
        for key in ("position_verified", "navigation_usable", "heading_available",
                    "camera_pose_corrected", "metric_scale_available", "motion_commands"):
            self.assertIs(result[key], False)
        self.assertFalse(result["sensor_rotation"]["camera_rotation_verified"])
        self.assertFalse(result["visual_comparison"]["available"])
        self.assertIsNone(result["timing"]["physical_capture_time_uncertainty_s"])

    def test_opposite_quaternion_sign_is_the_same_orientation(self):
        self.observe(100.0, degrees=47.0, accept=True)
        value = envelope(100.2, degrees=47.0)
        value["sensors"]["attitude"]["data"]["quaternion_wxyz"] = [-x for x in quaternion(47.0)]
        result, _, _ = self.observe(100.2, value=value)
        self.assertAlmostEqual(result["sensor_rotation"]["angle_rad"], 0.0, places=7)

    def test_snapshot_is_selected_at_decode_tick_and_frozen_for_same_frame(self):
        result, captured, visual = self.observe(100.0)
        self.assertEqual(self.source.calls, [100.0])
        self.source.value = envelope(100.1, degrees=90.0)
        second = self.tracking.evidence(captured, visual)
        self.assertEqual(result, second)
        self.assertEqual(self.source.calls, [100.0])
        second["sensors"]["imu"]["accel_m_s2"][0] = 999.0
        self.assertNotEqual(second, self.tracking.evidence(captured, visual))

    def test_future_receipt_is_not_used_even_when_source_breaks_causal_contract(self):
        self.observe(100.0, accept=True)
        result, _, _ = self.observe(100.2, value=envelope(100.3, degrees=90.0))
        self.assertFalse(result["available"])
        self.assertEqual(result["state"], "source_timing_or_identity_invalid")
        result, _, _ = self.observe(100.4, degrees=180.0)
        self.assertFalse(result["sensor_rotation"]["available"])

    def test_repeated_measurement_ignores_new_http_sequence_and_receive_time(self):
        self.observe(100.0, accept=True)
        value = envelope(100.2, degrees=60.0, sample_count=5000, observed_ms=100000)
        value["sequence"] = 100000
        result, _, _ = self.observe(100.2, value=value, accept=True)
        self.assertEqual(result["sensor_rotation"]["reason"], "reused_sample")
        self.assertFalse(result["sensors"]["attitude"]["new_sample"])
        result, _, _ = self.observe(100.4, degrees=90.0)
        self.assertAlmostEqual(result["sensor_rotation"]["angle_deg"], 90.0)
        self.assertEqual(result["sensor_rotation"]["previous_frame_id"], "frame-100.0")

    def test_unpublished_frame_does_not_become_a_reference_or_bridge_a_gap(self):
        self.observe(100.0, accept=True)
        result, _, _ = self.observe(100.2, degrees=45.0)
        self.assertTrue(result["sensor_rotation"]["available"])
        result, _, _ = self.observe(100.4, degrees=90.0, accept=True)
        self.assertFalse(result["sensor_rotation"]["available"])
        self.assertEqual(result["sensor_rotation"]["reason"], "previous_observation_not_accepted")
        result, _, _ = self.observe(100.6, degrees=120.0)
        self.assertAlmostEqual(result["sensor_rotation"]["angle_deg"], 30.0)
        self.assertEqual(result["sensor_rotation"]["previous_frame_id"], "frame-100.4")

    def test_acceptance_requires_exact_pending_frame_once(self):
        _, captured, visual = self.observe(100.0)
        wrong = {**captured, "frame_id": "other"}
        self.assertFalse(self.tracking.record_pose(wrong, localization(wrong)))
        self.assertFalse(self.tracking.record_pose(captured, {**visual, "map_id": "other"}))
        self.assertTrue(self.tracking.record_pose(captured, visual))
        self.assertFalse(self.tracking.record_pose(captured, visual))
        self.assertEqual(self.tracking.status()["accepted_observations"], 1)

    def test_attitude_epoch_changes_never_produce_a_rotation_delta(self):
        for change in ({"receiver": "other"}, {"boot": "other"},
                       {"generation": 2}, {"origin": 1001}):
            with self.subTest(change=change):
                self.tracking.set_context("map-one", "cal-one", "session-one")
                self.observe(100.0, accept=True)
                result, _, _ = self.observe(100.2, degrees=90.0, **change)
                self.assertFalse(result["sensor_rotation"]["available"])
                self.assertEqual(result["sensor_rotation"]["reason"], "sensor_reference_changed")

    def test_map_calibration_session_and_stream_changes_reset_comparisons(self):
        for context in (("map-two", "cal-one", "session-one"),
                        ("map-one", "cal-two", "session-one"),
                        ("map-one", "cal-one", "session-two")):
            with self.subTest(context=context):
                self.tracking.set_context("map-one", "cal-one", "session-one")
                self.observe(100.0, accept=True)
                self.tracking.set_context(*context)
                result, _, _ = self.observe(
                    100.2, degrees=90.0,
                    frame_changes={"calibration_id": context[1]},
                    localization_changes={"map_id": context[0]})
                self.assertFalse(result["sensor_rotation"]["available"])
        self.tracking.set_context("map-one", "cal-one", "session-one")
        self.observe(100.0, accept=True)
        result, _, _ = self.observe(100.2, degrees=90.0,
                                   frame_changes={"stream_epoch": "stream-two"})
        self.assertEqual(result["sensor_rotation"]["reason"], "stream_changed")

    def test_visual_tracking_loss_keeps_independent_native_sensor_evidence(self):
        self.observe(100.0, accept=True, localization_changes={"valid": False})
        result, _, _ = self.observe(100.2, degrees=20.0,
                                   localization_changes={"valid": False, "T_map_camera": None})
        self.assertTrue(result["sensor_rotation"]["available"])
        self.assertFalse(result["position_verified"])
        self.assertFalse(result["camera_pose_corrected"])

    def test_stale_or_paused_sensor_interrupts_only_qualified_continuity(self):
        for scenario in ("stale", "paused", "imu_invalid"):
            with self.subTest(scenario=scenario):
                self.tracking.set_context("map-one", "cal-one", "session-one")
                self.observe(100.0, accept=True)
                value = envelope(100.2, degrees=30.0)
                if scenario == "stale":
                    value["sensors"]["attitude"].update(
                        valid=False, age_ms=501.0, age_upper_bound_ms=501.0, reason="stale")
                elif scenario == "paused":
                    value["telemetry"]["ahrs"]["paused"] = True
                else:
                    value["sensors"]["imu"]["valid"] = False
                result, _, _ = self.observe(100.2, value=value, accept=True)
                self.assertFalse(result["sensors"]["attitude"]["valid"])
                result, _, _ = self.observe(100.4, degrees=60.0)
                self.assertFalse(result["sensor_rotation"]["available"])

    def test_freshness_upper_bound_cannot_be_hidden_by_low_reported_age(self):
        value = envelope(100.0)
        value["sensors"]["attitude"].update(age_ms=10.0, age_upper_bound_ms=501.0)
        result, _, _ = self.observe(100.0, value=value)
        self.assertFalse(result["sensors"]["attitude"]["valid"])
        self.assertTrue(result["sensors"]["barometer"]["valid"])

    def test_missing_telemetry_and_source_failure_break_continuity_without_raising(self):
        for failure in (None, "snapshot"):
            with self.subTest(failure=failure):
                self.tracking.set_context("map-one", "cal-one", "session-one")
                self.observe(100.0, accept=True)
                self.source.value = None
                self.source.fail = failure
                result = self.tracking.evidence(frame(100.2), localization(frame(100.2)))
                self.assertFalse(result["available"])
                self.source.fail = None
                result, _, _ = self.observe(100.4, degrees=60.0)
                self.assertFalse(result["sensor_rotation"]["available"])

    def test_host_and_sensor_gaps_are_explicit(self):
        self.observe(100.0, accept=True)
        result, _, _ = self.observe(100.6, degrees=90.0)
        self.assertEqual(result["sensor_rotation"]["reason"], "evidence_gap")
        self.tracking.set_context("map-one", "cal-one", "session-one")
        self.observe(100.0, accept=True)
        result, _, _ = self.observe(100.2, degrees=90.0, observed_ms=100700)
        self.assertEqual(result["sensor_rotation"]["reason"], "evidence_gap")

    def test_backward_frame_clock_cannot_bridge_to_accepted_orientation(self):
        self.observe(100.0, accept=True)
        result, _, _ = self.observe(99.9, degrees=45.0, accept=True)
        self.assertEqual(result["sensor_rotation"]["reason"], "frame_order_invalid")
        result, _, _ = self.observe(100.1, degrees=60.0)
        self.assertAlmostEqual(result["sensor_rotation"]["angle_deg"], 15.0)

    def test_wrapped_firmware_sample_clock_and_saturated_counter_are_valid(self):
        self.observe(100.0, accept=True, observed_ms=(1 << 32) - 100,
                     sample_count=(1 << 32) - 1)
        result, _, _ = self.observe(100.2, degrees=30.0, observed_ms=100,
                                   sample_count=(1 << 32) - 1)
        self.assertAlmostEqual(result["sensor_rotation"]["angle_deg"], 30.0)
        self.assertAlmostEqual(result["sensor_rotation"]["interval_s"], 0.2)

    def test_out_of_order_sensor_counter_is_not_a_new_rotation_interval(self):
        self.observe(100.0, accept=True)
        result, _, _ = self.observe(100.2, degrees=30.0, sample_count=4999)
        self.assertEqual(result["sensor_rotation"]["reason"], "sample_order_invalid")

    def test_malformed_quaternion_never_creates_orientation_or_zero_substitute(self):
        for bad in ([0, 0, 0, 0], [2, 0, 0, 0], [1, 0, 0],
                    [True, 0, 0, 0], [float("nan"), 0, 0, 0], None):
            with self.subTest(bad=bad):
                value = envelope(100.0)
                value["sensors"]["attitude"]["data"]["quaternion_wxyz"] = bad
                self.tracking.set_context("map-one", "cal-one", "session-one")
                result, _, _ = self.observe(100.0, value=value)
                self.assertFalse(result["sensors"]["attitude"]["valid"])
                self.assertNotIn("quaternion_wxyz", result["sensors"]["attitude"])
                json.dumps(result, allow_nan=False)

    def test_unknown_native_frame_or_heading_contract_is_rejected(self):
        for field, invalid in (("frame", "camera"), ("reference", "north"),
                               ("heading_absolute", True), ("mounting_alignment_applied", True),
                               ("generation", None), ("yaw_origin_uptime_ms", None)):
            with self.subTest(field=field):
                value = envelope(100.0)
                value["sensors"]["attitude"]["data"][field] = invalid
                self.tracking.set_context("map-one", "cal-one", "session-one")
                result, _, _ = self.observe(100.0, value=value)
                self.assertFalse(result["sensors"]["attitude"]["valid"])
                self.assertFalse(result["heading_available"])
                self.assertFalse(result["camera_pose_corrected"])

    def test_invalid_vectors_preserve_independently_healthy_pressure_and_magnetometer(self):
        for bad in ([0, 0], [0, float("inf"), 1], [False, 0, 1],
                    [1.7e308, 1.7e308, 1.7e308], None):
            with self.subTest(bad=bad):
                value = envelope(100.0)
                value["sensors"]["imu"]["data"]["accel_m_s2"] = bad
                self.tracking.set_context("map-one", "cal-one", "session-one")
                result, _, _ = self.observe(100.0, value=value)
                self.assertEqual(result["state"], "partial")
                self.assertTrue(result["sensors"]["barometer"]["valid"])
                self.assertTrue(result["sensors"]["mag"]["valid"])
                self.assertFalse(result["sensors"]["imu"]["valid"])
                self.assertFalse(result["sensors"]["attitude"]["valid"])
                json.dumps(result, allow_nan=False)

    def test_pressure_change_is_only_standard_pressure_altitude(self):
        self.observe(100.0, accept=True, altitude=125.0)
        result, _, _ = self.observe(100.2, altitude=126.25)
        change = result["pressure_altitude_change"]
        self.assertAlmostEqual(change["delta_m"], 1.25)
        self.assertEqual(change["sea_level_reference_pa"], 101325)
        self.assertFalse(change["ground_clearance"])
        self.assertFalse(change["vertical_translation_verified"])
        self.assertFalse(change["weather_compensated"])
        self.assertFalse(result["metric_scale_available"])

    def test_unknown_altitude_reference_keeps_pressure_but_rejects_altitude_delta(self):
        self.observe(100.0, accept=True)
        value = envelope(100.2)
        value["sensors"]["barometer"]["data"]["sea_level_reference_pa"] = 100000
        result, _, _ = self.observe(100.2, value=value)
        self.assertTrue(result["sensors"]["barometer"]["valid"])
        self.assertIsNone(result["sensors"]["barometer"]["pressure_altitude_m"])
        self.assertFalse(result["pressure_altitude_change"]["available"])
        self.assertEqual(result["pressure_altitude_change"]["reason"],
                         "pressure_altitude_reference_unavailable")

    def test_gyro_bias_validity_is_independent_of_last_calibration_result(self):
        value = envelope(100.0)
        value["calibration"]["data"]["status"] = "motion_detected"
        result, _, _ = self.observe(100.0, value=value)
        self.assertEqual(result["calibration"]["status"], "motion_detected")
        self.assertTrue(result["calibration"]["gyro_bias_valid"])
        self.assertTrue(result["sensors"]["imu"]["gyro_bias_applied"])

    def test_invalid_frame_clears_the_same_frame_cache_and_context_mismatch_is_explicit(self):
        self.observe(100.0, accept=True)
        result = self.tracking.evidence({"frame_id": "bad"})
        self.assertEqual(result["state"], "invalid_frame")
        result = self.tracking.evidence(frame(100.0), localization(frame(100.0)))
        self.assertFalse(result["sensor_rotation"]["available"])
        result, _, _ = self.observe(100.2, frame_changes={"calibration_id": "wrong"})
        self.assertEqual(result["state"], "context_mismatch")

    def test_explicit_calibration_clears_pending_reference_and_preserves_http_errors(self):
        self.observe(100.0, accept=True)
        _, captured, visual = self.observe(100.2, degrees=30.0)
        self.assertEqual(self.tracking.calibrate_gyro()["status"], "queued")
        self.assertFalse(self.tracking.record_pose(captured, visual))
        result, _, _ = self.observe(100.4, degrees=60.0)
        self.assertFalse(result["sensor_rotation"]["available"])
        self.source.fail = "calibrate_http"
        with self.assertRaises(CameraHTTPError) as caught:
            self.tracking.calibrate_gyro()
        self.assertEqual(caught.exception.http_status, 409)
        self.assertEqual(caught.exception.response["status"], "already_pending")
        self.source.fail = "calibrate"
        with self.assertRaises(CameraHTTPError) as caught:
            self.tracking.calibrate_gyro()
        self.assertFalse(caught.exception.outcome_known)

    def test_status_start_and_close_errors_are_isolated(self):
        for operation in ("start", "status", "close"):
            with self.subTest(operation=operation):
                self.source.fail = operation
                if operation == "start":
                    self.tracking.start()
                elif operation == "close":
                    self.tracking.close()
                else:
                    self.assertEqual(self.tracking.status()["source"]["state"], "unavailable")
        self.source.fail = None
        self.tracking.start()
        self.assertTrue(self.source.started)

    def test_disabled_mode_never_starts_or_reads_source_during_frames(self):
        source = FakeSource()
        tracking = AHRSTracking({"url": "fixture://no-network"}, source=source)
        tracking.start()
        result = tracking.evidence(frame(100.0), localization(frame(100.0)))
        self.assertFalse(source.started)
        self.assertEqual(source.calls, [])
        self.assertEqual(result["state"], "disabled")
        self.assertEqual(result["sensors"], {})
        self.assertFalse(tracking.record_pose(frame(100.0)))
        self.assertEqual(tracking.calibrate_gyro()["status"], "queued")
        self.assertEqual(source.calibration_calls, 1)
        tracking.close()

    def test_retention_is_bounded_to_current_and_previous_endpoints(self):
        for index in range(60):
            self.observe(100.0 + index * 0.1, degrees=index, accept=True)
        status = self.tracking.status()
        self.assertEqual(status["accepted_observations"], 60)
        self.assertEqual(status["accepted_sensor_endpoints"], 4)
        self.assertFalse(status["pending_observation"])
        self.assertNotIn("telemetry", self.tracking._last_evidence)

    def test_same_frame_cache_cannot_bypass_context_binding(self):
        _, captured, visual = self.observe(100.0, accept=True)
        result = self.tracking.evidence(captured, {**visual, "map_id": "other"})
        self.assertEqual(result["state"], "context_mismatch")
        self.assertFalse(result["available"])

    def test_real_transport_contract_uses_causal_receipts_without_network_in_evidence(self):
        from ahrs_source import AHRSTelemetrySource
        from test_ahrs_source import Client, telemetry
        clock = [100.0]
        first = telemetry(1000)
        second = telemetry(1200, sample_step=10)
        second["ahrs"]["attitude"].update(quaternion_wxyz=quaternion(30.0), yaw_deg=30.0)
        second["ahrs"]["barometer"]["pressure_altitude_m"] += 0.25
        client = Client(first, second)
        source = AHRSTelemetrySource(
            {"url": "fixture://no-network"}, {"enabled": True}, client=client,
            monotonic=lambda: clock[0], utc_clock=lambda: "receipt-fixture")
        tracking = AHRSTracking({"url": "fixture://no-network"},
                                {"enabled": True}, source=source)
        self.addCleanup(tracking.close)
        tracking.set_context("map-one", "cal-one", "session-one")
        self.assertTrue(source.poll_once())
        captured = frame(100.0)
        result = tracking.evidence(captured, localization(captured))
        self.assertEqual(result["state"], "available")
        self.assertEqual(result["sensors"]["imu"]["sample_count"], 10)
        self.assertEqual(result["sensors"]["attitude"]["sample_count"], 10)
        self.assertTrue(tracking.record_pose(captured, localization(captured)))
        clock[0] = 100.2
        self.assertTrue(source.poll_once())
        # The second request completed after this frame: use the first receipt.
        captured = frame(100.1)
        result = tracking.evidence(captured, localization(captured))
        self.assertEqual(result["sensor_rotation"]["reason"], "reused_sample")
        self.assertEqual(result["timing"]["received_monotonic_s"], 100.0)
        self.assertTrue(tracking.record_pose(captured, localization(captured)))
        captured = frame(100.2)
        result = tracking.evidence(captured, localization(captured))
        self.assertAlmostEqual(result["sensor_rotation"]["angle_deg"], 30.0, places=5)
        self.assertAlmostEqual(result["pressure_altitude_change"]["delta_m"], 0.25)
        self.assertEqual(result["sensors"]["attitude"]["observed_uptime_ms"], 1180)
        self.assertEqual(client.calls, 2)
        self.assertEqual(client.posts, 0)
        json.dumps(result, allow_nan=False)

    def test_default_real_source_constructor_accepts_disabled_fixture_camera(self):
        tracking = AHRSTracking({"url": "fixture://no-network"})
        self.addCleanup(tracking.close)
        tracking.start()
        self.assertEqual(tracking.evidence(frame(100.0))["state"], "disabled")
        self.assertIsNone(tracking.snapshot())


if __name__ == "__main__":
    unittest.main()
