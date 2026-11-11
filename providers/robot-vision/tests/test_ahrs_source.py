"""AHRS wire contract, causal cache, sensor isolation, and one-shot calibration."""
import copy
import io
import json
import threading
import unittest
from urllib.error import URLError

from ahrs_source import AHRSTelemetrySource, FRESHNESS_MS, validate_ahrs_snapshot
from camera_telemetry import CameraHTTPClient, CameraHTTPError, MAX_RESPONSE_BYTES, UINT32_MAX
from test_camera_telemetry import light, snapshot as radio_snapshot


def telemetry(uptime_ms=1000, boot_id="boot-a", sequence=0, sample_step=0):
    def health(age, count, address, identity):
        return {
            "present": True, "valid": True, "age_ms": age,
            "sample_count": count + sample_step, "observed_uptime_ms": (uptime_ms - age) & UINT32_MAX,
            "address": address, "who_am_i": identity, "error": None,
            "i2c_error": 0, "error_count": 0,
        }
    imu = health(20, 10, 0x68, 0x71)
    imu.update(frame="mpu9250_native_xyz", accel_m_s2=[0.0, 0.0, 9.807],
               gyro_rad_s=[0.01, 0.02, 0.03], gyro_raw_rad_s=[0.02, 0.03, 0.04],
               gyro_bias_applied=True, accelerometer_calibrated=False, temp_c=25.0)
    mag = health(40, 8, 0x0c, 0x48)
    mag.update(frame="ak8963_native_xyz", field_uT=[15.0, 20.0, 30.0],
               calibration="factory_sensitivity_only", used_for_heading=False)
    baro = health(80, 5, 0x76, 0x58)
    baro.update(pressure_pa=101100.0, temp_c=24.0, pressure_altitude_m=18.75,
                sea_level_reference_pa=101325, altitude_reference="ISA_standard_pressure_not_AGL")
    return {
        "schema": 1, "type": "ahrs", "receiver_id": "esp32cam-test", "boot_id": boot_id,
        "uptime_ms": uptime_ms, "sequence": sequence, "timestamp_ms": None,
        "ahrs": {
            "worker_ready": True, "paused": False, "hardware_verified": False,
            "freshness_ms": dict(FRESHNESS_MS), "imu": imu, "mag": mag, "barometer": baro,
            "attitude": {
                "valid": True, "age_ms": 20, "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
                "roll_deg": 0.0, "pitch_deg": 0.0, "yaw_deg": 0.0,
                "reference": "relative_yaw", "generation": 1, "yaw_origin_uptime_ms": 100,
                "frame": "MPU_axes_to_gravity_aligned_initial_yaw",
                "heading_absolute": False, "mounting_alignment_applied": False,
                "reset_reason": "initialized",
            },
            "calibration": {
                "status": "succeeded", "gyro_bias_valid": True,
                "gyro_bias_rad_s": [0.01, 0.01, 0.01], "samples": 100,
                "window_ms": 2000, "persistent": False,
            },
        },
    }


def queued():
    return {"status": "queued", "accepted": True, "http_status": 202,
            "outcome_known": True, "completed": False, "completion_correlated": False,
            "camera_response": {"status": "queued"}}


class Response(io.BytesIO):
    def __init__(self, value, code=200):
        self.code = code
        super().__init__(value if isinstance(value, bytes) else json.dumps(value).encode("utf-8"))

    def getcode(self):
        return self.code


class Opener:
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class Client:
    network_timeout_s = 0.2

    def __init__(self, *values, calibration_result=None):
        self.values, self.calls, self.posts = list(values), 0, 0
        self.calibration_result = queued() if calibration_result is None else calibration_result

    def get_ahrs(self):
        self.calls += 1
        result = self.values.pop(0)
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)

    def calibrate_gyro(self):
        self.posts += 1
        if isinstance(self.calibration_result, Exception):
            raise self.calibration_result
        return copy.deepcopy(self.calibration_result)


class HTTPTests(unittest.TestCase):
    def client(self, opener):
        return CameraHTTPClient(
            {"url": "rtsp://user:secret@camera.example:554/stream"}, opener=opener)

    def test_get_ahrs_uses_camera_auth_and_keeps_raw_fields(self):
        value = telemetry()
        opener = Opener(Response(value))
        result = self.client(opener).get_ahrs()
        self.assertEqual(result, value)
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, "http://camera.example/api/ahrs")
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNotNone(request.get_header("Authorization"))
        self.assertEqual(timeout, 2.0)

    def test_calibration_202_is_queued_acceptance_only_once(self):
        opener = Opener(Response({"status": "queued"}, 202))
        result = self.client(opener).calibrate_gyro()
        request, _ = opener.requests[0]
        self.assertEqual(request.full_url, "http://camera.example/api/ahrs/calibrate")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.data, b"{}")
        self.assertLessEqual(len(request.data), 256)
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertTrue(result["accepted"])
        self.assertTrue(result["outcome_known"])
        self.assertFalse(result["completed"])
        self.assertFalse(result["completion_correlated"])
        self.assertEqual(result["camera_response"], {"status": "queued"})
        self.assertEqual(len(opener.requests), 1)

    def test_known_rejections_and_unknown_receipts_do_not_retry(self):
        cases = [
            (Response({"error": "busy"}, 409), 409, True),
            (Response({"error": "unavailable"}, 503), 503, True),
            (Response(b"missing", 404), 404, True),
            (Response({"status": "queued"}, 200), 200, False),
            (Response(b"{broken", 202), 202, False),
            (Response({"status": "succeeded"}, 202), 202, False),
            (Response({}, 500), 500, False),
            (URLError("secret http://user:secret@camera.example"), None, False),
        ]
        for response, status, known in cases:
            with self.subTest(status=status, known=known):
                opener = Opener(response)
                with self.assertRaises(CameraHTTPError) as caught:
                    self.client(opener).calibrate_gyro()
                error = caught.exception.as_dict()
                self.assertEqual(error["http_status"], status)
                self.assertEqual(error["outcome_known"], known)
                self.assertNotIn("secret", json.dumps(error))
                self.assertEqual(len(opener.requests), 1)

    def test_old_ahrs_endpoint_does_not_disable_radio_or_light(self):
        opener = Opener(Response(b"not found", 404), Response(radio_snapshot()), Response(light()))
        client = self.client(opener)
        with self.assertRaises(CameraHTTPError) as caught:
            client.get_ahrs()
        self.assertEqual(caught.exception.http_status, 404)
        self.assertEqual(client.get_telemetry(), radio_snapshot())
        self.assertEqual(client.get_light(), light())

    def test_bounded_strict_json_applies_to_ahrs(self):
        invalid = [b" " * (MAX_RESPONSE_BYTES + 1), b'{"value":NaN}',
                   b'{"value":1e999}', b'{"value":1e309}', b'{"schema":1,"schema":1}']
        for body in invalid:
            with self.subTest(body=body[:40]), self.assertRaises(CameraHTTPError):
                self.client(Opener(Response(body))).get_ahrs()


class CacheTests(unittest.TestCase):
    def source(self, client, clock=None, **config):
        current = [100.0] if clock is None else clock
        settings = {"enabled": True}
        settings.update(config)
        return AHRSTelemetrySource(
            {"url": "rtsp://user:secret@camera.example/stream", "source_id": "mounted-camera"},
            settings, client=client, monotonic=lambda: current[0], utc_clock=lambda: "receipt-utc")

    def test_valid_envelope_preserves_raw_data_and_independent_sensor_ages(self):
        clock = [100.0]
        raw = telemetry()
        source = self.source(Client(raw), clock)
        self.assertTrue(source.poll_once())
        clock[0] += 0.6
        result = source.snapshot()
        self.assertEqual(result["telemetry"], raw)
        self.assertEqual(result["sensors"]["imu"]["reason"], "stale")
        self.assertFalse(result["sensors"]["attitude"]["valid"])
        self.assertTrue(result["sensors"]["mag"]["valid"])
        self.assertTrue(result["sensors"]["barometer"]["valid"])
        self.assertAlmostEqual(result["sensors"]["mag"]["age_ms"], 640.0)
        self.assertAlmostEqual(result["sensors"]["barometer"]["age_ms"], 680.0)
        result["telemetry"]["ahrs"]["imu"]["accel_m_s2"][2] = 0
        self.assertEqual(source.snapshot()["telemetry"]["ahrs"]["imu"]["accel_m_s2"][2], 9.807)
        self.assertNotIn("fresh", result)

    def test_sensor_malformed_boundaries_keep_other_sensors(self):
        cases = [
            ("imu", "gyro_rad_s", [True, 0, 0]),
            ("imu", "accel_m_s2", ["inf", 0, 0]),
            ("mag", "field_uT", [0, 0]),
            ("mag", "age_ms", True),
            ("barometer", "pressure_pa", -10),
            ("barometer", "sea_level_reference_pa", True),
            ("attitude", "quaternion_wxyz", [2, 0, 0, 0]),
            ("attitude", "quaternion_wxyz", [0, 0, 0, 0]),
            ("attitude", "generation", True),
            ("attitude", "heading_absolute", True),
            ("attitude", "reference", "north"),
        ]
        for sensor, field, invalid in cases:
            with self.subTest(sensor=sensor, field=field):
                raw = telemetry()
                raw["ahrs"][sensor][field] = invalid
                source = self.source(Client(raw))
                self.assertTrue(source.poll_once())
                result = source.snapshot()
                self.assertFalse(result["sensors"][sensor]["valid"])
                healthy = "mag" if sensor == "barometer" else "barometer"
                self.assertTrue(result["sensors"][healthy]["valid"])
                if sensor == "imu":
                    self.assertFalse(result["sensors"]["attitude"]["valid"])

    def test_nonfinite_injected_payload_is_rejected_without_contaminating_cache(self):
        for invalid in (float("inf"), float("-inf"), float("nan")):
            for field in ("measurement", "additive_metadata"):
                with self.subTest(value=str(invalid), field=field):
                    raw = telemetry(uptime_ms=1100, sample_step=1)
                    if field == "measurement":
                        raw["ahrs"]["imu"]["accel_m_s2"][0] = invalid
                    else:
                        raw["diagnostics"] = {"nested": [invalid]}
                    source = self.source(Client(telemetry(), raw))
                    self.assertTrue(source.poll_once())
                    self.assertFalse(source.poll_once())
                    cached = source.snapshot()
                    self.assertEqual(cached["telemetry"]["uptime_ms"], 1000)
                    json.dumps(cached, allow_nan=False)
                    json.dumps(source.status(), allow_nan=False)
                    self.assertEqual(source.status()["snapshots_accepted"], 1)

    def test_accepted_raw_and_normalized_snapshot_are_strict_json_serializable(self):
        raw = telemetry()
        raw["ahrs"]["mag"]["field_uT"] = ["malformed", 1, 2]
        source = self.source(Client(raw))
        self.assertTrue(source.poll_once())
        cached = source.snapshot()
        self.assertEqual(cached["telemetry"], raw)
        self.assertFalse(cached["sensors"]["mag"]["valid"])
        self.assertTrue(cached["sensors"]["imu"]["valid"])
        self.assertEqual(json.loads(json.dumps(cached, allow_nan=False)), cached)
        self.assertEqual(json.loads(json.dumps(source.status(), allow_nan=False)), source.status())

    def test_null_unseen_measurements_are_not_zeros_or_fresh_data(self):
        raw = telemetry()
        imu = raw["ahrs"]["imu"]
        imu.update(present=False, valid=False, age_ms=None, observed_uptime_ms=None, sample_count=0,
                   accel_m_s2=None, gyro_rad_s=None, gyro_raw_rad_s=None, temp_c=None,
                   error="not_detected", i2c_error=-3)
        attitude = raw["ahrs"]["attitude"]
        attitude.update(valid=False, age_ms=None, quaternion_wxyz=None,
                        roll_deg=None, pitch_deg=None, yaw_deg=None, generation=0, yaw_origin_uptime_ms=None)
        source = self.source(Client(raw))
        self.assertTrue(source.poll_once())
        result = source.snapshot()
        self.assertTrue(result["sensors"]["imu"]["structurally_valid"])
        self.assertFalse(result["sensors"]["imu"]["valid"])
        self.assertIsNone(result["sensors"]["imu"]["sample_identity"])
        self.assertIsNone(result["sensors"]["imu"]["data"]["accel_m_s2"])
        self.assertTrue(result["sensors"]["barometer"]["valid"])

    def test_firmware_flags_and_calibration_validation_are_separate(self):
        for change, reason in (("paused", "paused"), ("worker_ready", "worker_not_ready")):
            raw = telemetry()
            raw["ahrs"][change] = change == "paused"
            source = self.source(Client(raw))
            source.poll_once()
            self.assertTrue(all(s["reason"] == reason for s in source.snapshot()["sensors"].values()))
        raw = telemetry()
        raw["ahrs"]["calibration"]["status"] = "invented-success"
        source = self.source(Client(raw))
        source.poll_once()
        self.assertFalse(source.snapshot()["calibration"]["valid"])
        self.assertTrue(source.snapshot()["sensors"]["imu"]["valid"])

    def test_causal_history_ignores_receipts_after_frame_and_bounds_retention(self):
        clock = [100.0]
        source = self.source(Client(
            telemetry(), telemetry(uptime_ms=1100, sample_step=1),
            telemetry(uptime_ms=1200, sample_step=2)), clock,
            history_capacity=2, history_retention_s=1)
        source.poll_once()
        self.assertIsNone(source.snapshot(at_monotonic=99.9))
        clock[0] = 100.1
        source.poll_once()
        clock[0] = 100.2
        source.poll_once()
        self.assertIsNone(source.snapshot(at_monotonic=100.05))  # evicted by capacity
        historic = source.snapshot(at_monotonic=100.15)
        self.assertEqual(historic["telemetry"]["uptime_ms"], 1100)
        self.assertAlmostEqual(historic["sensors"]["imu"]["effective_age_ms"], 70)
        self.assertIsNone(source.snapshot(at_monotonic=100.3))  # future query
        self.assertEqual(source.status()["history_size"], 2)
        clock[0] = 101.3
        self.assertIsNone(source.snapshot())
        self.assertEqual(source.status()["history_size"], 0)

    def test_http_sequence_is_not_sample_identity_and_duplicates_never_refresh(self):
        clock = [100.0]
        first = telemetry()
        republished = copy.deepcopy(first)
        republished.update(uptime_ms=1100, sequence=300)
        source = self.source(Client(first, first, republished,
                                    telemetry(uptime_ms=1200, sample_step=1, sequence=300)), clock)
        source.poll_once()
        original = source.snapshot()["sensors"]["imu"]["sample_identity"]
        clock[0] = 100.1
        self.assertFalse(source.poll_once())
        self.assertEqual(source.snapshot()["received_monotonic"], 100.0)
        self.assertTrue(source.poll_once())
        repeated = source.snapshot()["sensors"]["imu"]
        self.assertEqual(repeated["sample_identity"], original)
        self.assertTrue(repeated["repeat_sample"])
        self.assertAlmostEqual(repeated["age_ms"], 120)
        clock[0] = 100.2
        self.assertTrue(source.poll_once())
        self.assertNotEqual(source.snapshot()["sensors"]["imu"]["sample_identity"], original)
        self.assertEqual(source.status()["duplicate_snapshots"], 1)

    def test_same_uptime_can_include_new_streamed_sample(self):
        first, second = telemetry(), telemetry(sample_step=1)
        second["ahrs"]["imu"]["observed_uptime_ms"] += 1
        second["ahrs"]["imu"]["age_ms"] -= 1
        source = self.source(Client(first, second))
        self.assertTrue(source.poll_once())
        identity = source.snapshot()["sensors"]["imu"]["sample_identity"]
        self.assertTrue(source.poll_once())
        self.assertNotEqual(source.snapshot()["sensors"]["imu"]["sample_identity"], identity)

    def test_uptime_wrap_reboot_and_retired_boot_do_not_restore_old_epoch(self):
        source = self.source(Client(
            telemetry(uptime_ms=UINT32_MAX-5),
            telemetry(uptime_ms=30, sample_step=1),
            telemetry(uptime_ms=UINT32_MAX-4, sample_step=1),
            telemetry(boot_id="boot-b", uptime_ms=100),
            telemetry(boot_id="boot-a", uptime_ms=1000, sample_step=2)))
        self.assertTrue(source.poll_once())
        self.assertTrue(source.poll_once())
        self.assertTrue(source.snapshot()["sensors"]["imu"]["valid"])
        self.assertFalse(source.poll_once())
        self.assertTrue(source.poll_once())
        self.assertFalse(source.poll_once())
        self.assertEqual(source.status()["boot_id"], "boot-b")
        self.assertEqual(source.status()["replayed_snapshots"], 2)
        self.assertEqual(source.status()["boot_changes"], 1)

    def test_envelope_uptime_can_precede_streamed_observation(self):
        raw = telemetry()
        raw["ahrs"]["imu"].update(observed_uptime_ms=1005, age_ms=1)
        raw["ahrs"]["attitude"]["age_ms"] = 1
        source = self.source(Client(raw))
        self.assertTrue(source.poll_once())
        self.assertTrue(source.snapshot()["sensors"]["imu"]["valid"])
        self.assertTrue(source.snapshot()["sensors"]["attitude"]["valid"])

    def test_saturated_sample_counter_still_allows_new_observed_uptime(self):
        first, second = telemetry(), telemetry(uptime_ms=1100)
        for value in (first, second):
            value["ahrs"]["imu"]["sample_count"] = UINT32_MAX
        source = self.source(Client(first, second))
        source.poll_once()
        original = source.snapshot()["sensors"]["imu"]["sample_identity"]
        source.poll_once()
        result = source.snapshot()["sensors"]["imu"]
        self.assertTrue(result["valid"])
        self.assertNotEqual(result["sample_identity"], original)

    def test_sensor_counter_regression_does_not_poison_other_sensors(self):
        first, next_value = telemetry(), telemetry(uptime_ms=1100, sample_step=1)
        next_value["ahrs"]["mag"]["sample_count"] = 2
        source = self.source(Client(first, next_value))
        source.poll_once()
        source.poll_once()
        result = source.snapshot()["sensors"]
        self.assertEqual(result["mag"]["reason"], "replayed_sample_provenance")
        self.assertTrue(result["imu"]["valid"])
        self.assertTrue(result["barometer"]["valid"])

    def test_attitude_generation_changes_identity_and_rejects_regression(self):
        first = telemetry()
        changed = telemetry(uptime_ms=1100, sample_step=1)
        changed["ahrs"]["attitude"].update(generation=2, yaw_origin_uptime_ms=1080,
                                            reset_reason="gyro_calibration")
        regressed = telemetry(uptime_ms=1200, sample_step=2)
        source = self.source(Client(first, changed, regressed))
        source.poll_once()
        identity = source.snapshot()["sensors"]["attitude"]["sample_identity"]
        source.poll_once()
        self.assertNotEqual(source.snapshot()["sensors"]["attitude"]["sample_identity"], identity)
        self.assertTrue(source.snapshot()["sensors"]["attitude"]["valid"])
        source.poll_once()
        self.assertFalse(source.snapshot()["sensors"]["attitude"]["valid"])
        self.assertTrue(source.snapshot()["sensors"]["imu"]["valid"])

    def test_request_midpoint_is_approximate_and_rtt_counts_toward_staleness(self):
        clock = [100.0]
        class TimedClient(Client):
            def get_ahrs(self):
                clock[0] += 0.2
                return super().get_ahrs()
        source = self.source(TimedClient(telemetry()), clock)
        source.poll_once()
        result = source.snapshot()
        self.assertAlmostEqual(result["request_duration_s"], 0.2)
        imu = result["sensors"]["imu"]
        self.assertAlmostEqual(imu["observed_monotonic_s"], 100.08)
        self.assertAlmostEqual(imu["time_uncertainty_s"], 0.1)
        self.assertAlmostEqual(imu["age_upper_bound_ms"], 220)
        self.assertFalse(result["timing"]["timing_qualified"])
        self.assertFalse(result["timing"]["camera_exposure_time_bound"])
        clock[0] += 0.3
        self.assertFalse(source.snapshot()["sensors"]["imu"]["valid"])
        self.assertTrue(source.snapshot()["sensors"]["barometer"]["valid"])

    def test_envelope_malformed_or_failed_poll_retains_only_aging_previous_data(self):
        clock = [100.0]
        invalid = telemetry(uptime_ms=1100)
        invalid["boot_id"] = ""
        source = self.source(Client(
            telemetry(), invalid, CameraHTTPError("camera_http_endpoint_unavailable", http_status=404)), clock)
        self.assertTrue(source.poll_once())
        clock[0] += 3
        self.assertFalse(source.poll_once())
        self.assertFalse(source.snapshot()["valid"])
        self.assertFalse(source.poll_once())
        self.assertFalse(source.status()["supported"])
        self.assertEqual(source.status()["state"], "disconnected")
        self.assertNotIn("secret", json.dumps(source.status()))

    def test_status_observation_identity_and_age_follow_selected_snapshot_during_poll(self):
        clock = [100.0]
        first = telemetry()
        second = telemetry(boot_id="boot-b", uptime_ms=100)
        second["receiver_id"] = "esp32cam-second"
        source = self.source(Client(first, second), clock)
        self.assertTrue(source.poll_once())
        first_identity = source.snapshot()["sensors"]["imu"]["sample_identity"]
        select_snapshot = source.snapshot

        def complete_poll_before_snapshot(at_monotonic=None):
            # status() already copied aggregate counters and the old latest
            # pointer; simulate the worker accepting B before cache selection.
            clock[0] = 100.2
            self.assertTrue(source.poll_once())
            clock[0] = 100.25
            return select_snapshot(at_monotonic=at_monotonic)

        source.snapshot = complete_poll_before_snapshot
        result = source.status()
        source.snapshot = select_snapshot
        selected = source.snapshot()
        self.assertEqual(result["receiver_id"], "esp32cam-second")
        self.assertEqual(result["boot_id"], "boot-b")
        self.assertAlmostEqual(result["last_receive_age_s"], 0.05)
        self.assertEqual(result["sensors"]["imu"]["sample_identity"],
                         selected["sensors"]["imu"]["sample_identity"])
        self.assertNotEqual(result["sensors"]["imu"]["sample_identity"], first_identity)
        self.assertAlmostEqual(result["sensors"]["imu"]["effective_age_ms"], 70.0)
        self.assertEqual(result["calibration"], selected["calibration"])

    def test_expired_status_retains_last_identity_only_with_empty_sensor_data(self):
        clock = [100.0]
        source = self.source(Client(telemetry()), clock, history_retention_s=1)
        source.poll_once()
        clock[0] = 102.0
        result = source.status()
        self.assertEqual(result["boot_id"], "boot-a")
        self.assertEqual(result["last_receive_age_s"], 2.0)
        self.assertEqual(result["sensors"], {})
        self.assertIsNone(result["calibration"])

    def test_envelope_types_cannot_coerce_booleans_or_absent_epoch(self):
        for changes in ({"schema": True}, {"type": "wifi"}, {"uptime_ms": True},
                        {"sequence": -1}, {"boot_id": ""}, {"timestamp_ms": 1.5}):
            value = telemetry()
            value.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_ahrs_snapshot(value)

    def test_disabled_source_never_polls_but_explicit_calibration_is_independent(self):
        client = Client()
        source = AHRSTelemetrySource({"url": "/tmp/fixture.mp4"}, client=client)
        self.assertEqual(source.poll_interval_s, 0.1)
        source.start()
        self.assertFalse(source.poll_once())
        self.assertIsNone(source.snapshot())
        self.assertEqual(source.status()["state"], "disabled")
        self.assertEqual(client.calls, 0)
        self.assertEqual(client.posts, 0)
        self.assertTrue(source.calibrate_gyro()["accepted"])
        self.assertEqual(client.posts, 1)
        source.close()
        with self.assertRaises(CameraHTTPError):
            source.calibrate_gyro()
        self.assertEqual(client.posts, 1)

    def test_later_calibration_status_does_not_complete_or_correlate_request(self):
        client = Client(telemetry())
        source = self.source(client)
        receipt = source.calibrate_gyro()
        source.poll_once()
        self.assertEqual(source.snapshot()["calibration"]["data"]["status"], "succeeded")
        last_request = source.status()["calibration_request"]
        self.assertEqual(last_request["status"], "queued")
        self.assertFalse(last_request["completed"])
        self.assertFalse(last_request["completion_correlated"])
        self.assertEqual(client.posts, 1)
        self.assertEqual(receipt["correlation"], "firmware_has_no_request_identifier")

    def test_unknown_calibration_receipt_is_retained_without_retry(self):
        client = Client(telemetry(), calibration_result=CameraHTTPError(
            "camera_http_transport_failed", outcome_known=False))
        source = self.source(client)
        with self.assertRaises(CameraHTTPError):
            source.calibrate_gyro()
        source.poll_once()
        receipt = source.status()["calibration_request"]
        self.assertEqual(receipt["status"], "unknown")
        self.assertIsNone(receipt["accepted"])
        self.assertFalse(receipt["completed"])
        self.assertEqual(client.posts, 1)

    def test_rejected_calibration_and_malformed_client_receipt_are_distinct(self):
        for result, expected in (
                (CameraHTTPError("busy", http_status=409, outcome_known=True), "rejected"),
                ({"status": "queued"}, "unknown")):
            source = self.source(Client(calibration_result=result))
            with self.assertRaises(CameraHTTPError):
                source.calibrate_gyro()
            self.assertEqual(source.status()["calibration_request"]["status"], expected)
            self.assertEqual(source.client.posts, 1)

    def test_cache_reads_never_wait_for_slow_poll_or_calibration(self):
        for operation in ("poll", "calibrate"):
            with self.subTest(operation=operation):
                entered, release, completed = threading.Event(), threading.Event(), threading.Event()
                class BlockingClient(Client):
                    def get_ahrs(self):
                        entered.set()
                        release.wait(2)
                        return telemetry()
                    def calibrate_gyro(self):
                        entered.set()
                        release.wait(2)
                        self.posts += 1
                        return queued()
                source = self.source(BlockingClient(), poll_interval_s=30)
                worker = threading.Thread(target=source.poll_once if operation == "poll" else source.calibrate_gyro)
                worker.start()
                try:
                    self.assertTrue(entered.wait(1))
                    def read():
                        self.assertIsNone(source.snapshot())
                        self.assertEqual(source.status()["buffer"], "bounded_causal_history")
                        completed.set()
                    reader = threading.Thread(target=read)
                    reader.start()
                    self.assertTrue(completed.wait(1), "cache read blocked behind HTTP")
                    reader.join()
                    self.assertFalse(release.is_set())
                finally:
                    release.set()
                    worker.join()
                    source.close()


if __name__ == "__main__":
    unittest.main()
