"""Camera HTTP contracts, sample identity, and poll/vision thread separation."""
import base64
import copy
import io
import json
import threading
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request

from camera_telemetry import (
    CameraHTTPClient, CameraHTTPError, CameraTelemetrySource, MAX_LIGHT_BODY_BYTES,
    MAX_RESPONSE_BYTES, UINT32_MAX, _NoRedirect, encode_light_update,
    observation_id, validate_telemetry_snapshot,
)


def light(**changes):
    value = {
        "driver": "ws2812-rmt", "gpio": 48, "rmt_channel": 3, "pixels": 1,
        "color_order": "GRB", "ready": True, "hardware_verified": False,
        "optical_readback": False, "on": False, "brightness": 128,
        "rgb": [255, 255, 255], "last_transmitted_rgb": [0, 0, 0],
        "command_state_known": True, "last_command_succeeded": True,
        "last_command_ms": 12, "last_attempt_ms": 12, "error": None,
    }
    value.update(changes)
    return value


def snapshot(**changes):
    value = {
        "schema": 1, "receiver_id": "esp32cam-0123456789ab", "boot_id": "boot-a",
        "uptime_ms": 30000, "sequence": 0, "timestamp_ms": None, "paused": False,
        "capabilities": {"wifi": True, "ble": True, "ftm": True, "bluetooth_classic": False},
        "observation_ttl_ms": 300000, "config": {}, "status": {},
        "wifi": [{
            "id": "aa:bb:cc:dd:ee:ff", "address": "aa:bb:cc:dd:ee:ff", "name": "access point",
            "rssi_dbm": -64, "rssi_filtered_dbm": -63.5, "sample_count": 4,
            "age_ms": 2000, "observed_uptime_ms": 28000, "channel": 1,
            "ftm_responder": False, "distance_m": 1.52, "distance_method": "rssi_assumed",
            "reference_rssi_1m_dbm": -45, "path_loss_exponent": 2.5,
            "calibrated": False, "calibration_source": "configured_assumption",
        }],
        "ble": [],
        "ftm": {"status": "idle", "supported": True, "busy": False, "distance_m": None,
                "age_ms": None, "rtt_ns": None, "samples": 0},
    }
    value.update(changes)
    return value


class Response(io.BytesIO):
    def __init__(self, value, code=200):
        self.code = code
        super().__init__(value if isinstance(value, bytes) else json.dumps(value).encode("utf-8"))

    def getcode(self):
        return self.code


class Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class Client:
    network_timeout_s = 0.2

    def __init__(self, *values):
        self.values = list(values)
        self.calls = 0
        self.light_updates = []

    def get_telemetry(self):
        self.calls += 1
        result = self.values.pop(0)
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)

    def get_light(self):
        return light()

    def set_light(self, update):
        self.light_updates.append(copy.deepcopy(update))
        return light(**update)


class HTTPTests(unittest.TestCase):
    def test_rtsp_origin_basic_auth_and_percent_encoded_credentials(self):
        opener = Opener(Response(snapshot()))
        client = CameraHTTPClient(
            {"url": "rtsp://camera-user:p%40ss%3Aword@camera.example:554/mjpeg/1"}, opener=opener)
        self.assertEqual(client.get_telemetry()["sequence"], 0)
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, "http://camera.example/api/telemetry")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"),
                         "Basic " + base64.b64encode(b"camera-user:p@ss:word").decode("ascii"))
        self.assertEqual(timeout, 2.0)
        self.assertNotIn("p%40ss", request.full_url)

    def test_explicit_credentials_http_port_and_strict_post_headers(self):
        opener = Opener(Response(light(on=True, brightness=255)))
        client = CameraHTTPClient(
            {"url": "http://old:old@camera.example:8080/stream",
             "username": "new", "password": "secret"}, opener=opener)
        result = client.set_light({"on": True, "brightness": 255})
        request, _ = opener.requests[0]
        self.assertEqual(request.full_url, "http://camera.example:8080/api/light")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(request.data), {"on": True, "brightness": 255})
        self.assertFalse(result["hardware_verified"])
        self.assertFalse(result["optical_readback"])

    def test_missing_auth_returns_401_once_without_challenge_retry(self):
        opener = Opener(Response({"error": "Authentication required"}, 401))
        client = CameraHTTPClient({"url": "rtsp://camera.example/stream"}, opener=opener)
        with self.assertRaises(CameraHTTPError) as caught:
            client.get_telemetry()
        self.assertEqual(caught.exception.http_status, 401)
        self.assertEqual(caught.exception.code, "camera_http_authentication_required")
        self.assertIsNone(opener.requests[0][0].get_header("Authorization"))
        self.assertEqual(len(opener.requests), 1)

    def test_old_firmware_telemetry_404_does_not_disable_light_endpoint(self):
        opener = Opener(Response(b"<html>not found</html>", 404), Response(light()))
        client = CameraHTTPClient({"url": "rtsp://camera.example/stream"}, opener=opener)
        with self.assertRaises(CameraHTTPError) as caught:
            client.get_telemetry()
        self.assertEqual(caught.exception.http_status, 404)
        self.assertEqual(client.get_light(), light())
        self.assertEqual(len(opener.requests), 2)

    def test_transport_failure_post_is_unknown_redacted_and_not_retried(self):
        opener = Opener(URLError("secret at http://user:secret@camera.example/api/light"))
        client = CameraHTTPClient(
            {"url": "rtsp://user:secret@camera.example/stream"}, opener=opener)
        with self.assertRaises(CameraHTTPError) as caught:
            client.set_light({"on": False})
        error = caught.exception.as_dict()
        self.assertFalse(error["outcome_known"])
        self.assertFalse(error["command_state_known"])
        self.assertNotIn("secret", json.dumps(error))
        self.assertEqual(len(opener.requests), 1)

    def test_driver_failure_503_retains_unknown_command_and_exact_payload(self):
        failed = light(ready=False, command_state_known=False, last_command_succeeded=False,
                       error={"code": 263, "name": "ESP_ERR_TIMEOUT", "stage": "rmt_tx_complete"})
        error_response = HTTPError("http://camera.example/api/light", 503, "Unavailable", {},
                                   io.BytesIO(json.dumps(failed).encode("utf-8")))
        opener = Opener(error_response)
        client = CameraHTTPClient({"url": "rtsp://camera.example/stream"}, opener=opener)
        with self.assertRaises(CameraHTTPError) as caught:
            client.set_light({"brightness": 64})
        error = caught.exception.as_dict()
        self.assertEqual(error["http_status"], 503)
        self.assertEqual(error["camera_response"], failed)
        self.assertFalse(error["outcome_known"])
        self.assertFalse(error["command_state_known"])
        self.assertEqual(len(opener.requests), 1)

    def test_malformed_successful_post_receipt_keeps_effect_unknown(self):
        for body in (b"{broken", b'{"on":true}', b'{"error":"unexpected"}'):
            with self.subTest(body=body):
                opener = Opener(Response(body))
                client = CameraHTTPClient({"url": "rtsp://camera.example/stream"}, opener=opener)
                with self.assertRaises(CameraHTTPError) as caught:
                    client.set_light({"on": True})
                self.assertFalse(caught.exception.outcome_known)
                self.assertEqual(caught.exception.http_status, 200)
                self.assertEqual(len(opener.requests), 1)

    def test_light_validation_boundaries_preserve_partial_object(self):
        values = [{"on": False}, {"brightness": 0}, {"brightness": 255},
                  {"rgb": [0, 128, 255]}, {"on": True, "brightness": 255, "rgb": [255, 255, 255]}]
        for value in values:
            with self.subTest(value=value):
                encoded = encode_light_update(value)
                self.assertEqual(json.loads(encoded), value)
                self.assertLessEqual(len(encoded), MAX_LIGHT_BODY_BYTES)
        invalid = [None, [], {}, {"on": 1}, {"on": "true"}, {"brightness": True},
                   {"brightness": -1}, {"brightness": 256}, {"brightness": 1.0},
                   {"rgb": [0, 1]}, {"rgb": [0, 1, 2, 3]}, {"rgb": (0, 1, 2)},
                   {"rgb": [False, 0, 0]}, {"rgb": [0, 256, 0]}, {"rgb": [0, 0, -1]},
                   {"duration": 10}, {"on\u0000ignored": True}, {"brightness": 10**1000}]
        opener = Opener()
        client = CameraHTTPClient({"url": "rtsp://camera.example/stream"}, opener=opener)
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                client.set_light(value)
        self.assertEqual(opener.requests, [])

    def test_response_is_bounded_and_strict_json(self):
        invalid = [b"", b"\xff", b"[]", b'{"schema":1,"schema":1}',
                   b'{"value":NaN}', b'{"value":1e999}', b'{"value":"\\ud800"}',
                   b" " * (MAX_RESPONSE_BYTES + 1)]
        for body in invalid:
            with self.subTest(body=body[:50]):
                client = CameraHTTPClient({"url": "rtsp://camera.example/stream"},
                                          opener=Opener(Response(body)))
                with self.assertRaises(CameraHTTPError) as caught:
                    client.get_telemetry()
                self.assertEqual(caught.exception.code, "camera_http_invalid_response")

    def test_all_redirects_rejected_and_cross_host_override_precedes_request(self):
        request = Request("http://camera.example/api/light", data=b'{"on":true}',
                          headers={"Authorization": "Basic confidential"}, method="POST")
        handler = _NoRedirect()
        for status in (301, 302, 303, 307, 308):
            self.assertIsNone(handler.redirect_request(
                request, None, status, "redirect", {}, "http://other.example/api/light"))
        opener = Opener()
        client = CameraHTTPClient({"url": "rtsp://user:secret@camera.example/stream"},
                                  {"base_url": "http://other.example"}, opener=opener)
        with self.assertRaises(CameraHTTPError) as caught:
            client.set_light({"on": True})
        self.assertTrue(caught.exception.outcome_known)
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(opener.requests, [])

    def test_valid_base_url_can_select_camera_http_port_without_url_credentials(self):
        opener = Opener(Response(light()))
        client = CameraHTTPClient({"url": "rtsp://camera.example:554/stream"},
                                  {"base_url": "http://camera.example:8081"}, opener=opener)
        client.get_light()
        self.assertEqual(opener.requests[0][0].full_url, "http://camera.example:8081/api/light")


class IdentityTests(unittest.TestCase):
    def test_validate_identity_types_capacity_and_streamed_later_observation(self):
        value = snapshot(uptime_ms=28000)
        value["wifi"][0]["observed_uptime_ms"] = 28005
        self.assertIs(validate_telemetry_snapshot(value), value)
        mutations = [
            lambda v: v.update(schema=True), lambda v: v.update(schema=2),
            lambda v: v.update(boot_id=""), lambda v: v.update(receiver_id=""),
            lambda v: v.update(uptime_ms=-1), lambda v: v.update(uptime_ms=True),
            lambda v: v.update(uptime_ms=UINT32_MAX+1), lambda v: v.update(sequence=True),
            lambda v: v["wifi"][0].update(age_ms=True),
            lambda v: v["wifi"][0].update(observed_uptime_ms=-1),
            lambda v: v["wifi"][0].update(sample_count=0),
            lambda v: v["wifi"][0].update(address="not-a-mac"),
            lambda v: v["wifi"].append(copy.deepcopy(v["wifi"][0])),
            lambda v: v.update(wifi=[copy.deepcopy(v["wifi"][0]) for _ in range(33)]),
        ]
        for mutation in mutations:
            invalid = snapshot()
            mutation(invalid)
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                validate_telemetry_snapshot(invalid)

    def test_observation_identity_distinguishes_boot_source_type_and_new_sample(self):
        value = snapshot()
        row = value["wifi"][0]
        original = observation_id("source-a", value, "wifi", row)
        replay = copy.deepcopy(row)
        replay.update(age_ms=99999, name="a changed display name")
        self.assertEqual(original, observation_id("source-a", value, "wifi", replay))
        ids = {original, observation_id("source-b", value, "wifi", row)}
        ids.add(observation_id("source-a", dict(value, boot_id="boot-b"), "wifi", row))
        ids.add(observation_id("source-a", value, "wifi", dict(row, sample_count=5)))
        ids.add(observation_id("source-a", value, "wifi", dict(row, observed_uptime_ms=28001)))
        ids.add(observation_id("source-a", value, "ble", dict(row, address_type=0)))
        ids.add(observation_id("source-a", value, "ble", dict(row, address_type=1)))
        self.assertEqual(len(ids), 7)


class CacheTests(unittest.TestCase):
    def source(self, client, clock=None, **config):
        current = clock if clock is not None else [100.0]
        settings = {"enabled": True}
        settings.update(config)
        return CameraTelemetrySource(
            {"url": "rtsp://user:secret@camera.example/stream", "source_id": "mounted-camera"},
            settings, client=client, monotonic=lambda: current[0], utc_clock=lambda: "receive-utc")

    def test_http_sequence_does_not_block_fresh_snapshots_or_create_new_sample(self):
        first = snapshot()
        next_value = snapshot(uptime_ms=31000)
        next_value["wifi"][0]["age_ms"] = 3000
        clock = [100.0]
        source = self.source(Client(first, next_value), clock)
        self.assertTrue(source.poll_once())
        original = source.snapshot()["observations"][0]["source_observation_id"]
        clock[0] = 101.0
        self.assertTrue(source.poll_once())
        result = source.snapshot()
        self.assertEqual(result["telemetry"]["sequence"], 0)
        self.assertEqual(result["observations"][0]["source_observation_id"], original)
        self.assertEqual(result["observations"][0]["effective_age_ms"], 3000)
        self.assertEqual(source.status()["snapshots_accepted"], 2)

    def test_age_advances_duplicates_do_not_refresh_and_payload_remains_unaltered(self):
        clock = [100.0]
        first = snapshot()
        source = self.source(Client(first, first), clock)
        source.poll_once()
        clock[0] = 102.0
        result = source.snapshot()
        self.assertEqual(result["received_at"], "receive-utc")
        self.assertEqual(result["observations"][0]["effective_age_ms"], 4000)
        self.assertEqual(result["telemetry"], first)
        self.assertFalse(source.poll_once())
        self.assertEqual(source.snapshot()["observations"][0]["effective_age_ms"], 4000)
        self.assertEqual(source.snapshot()["received_monotonic"], 100.0)
        self.assertEqual(source.status()["duplicate_snapshots"], 1)
        result["telemetry"]["wifi"].clear()
        self.assertEqual(len(source.snapshot()["telemetry"]["wifi"]), 1)

    def test_same_boot_replay_uint32_wrap_reboot_and_retired_boot(self):
        source = self.source(Client(
            snapshot(uptime_ms=UINT32_MAX-5), snapshot(uptime_ms=3),
            snapshot(uptime_ms=UINT32_MAX-4),
            snapshot(boot_id="boot-b", uptime_ms=10, wifi=[]),
            snapshot(boot_id="boot-a", uptime_ms=20)))
        self.assertTrue(source.poll_once())
        self.assertTrue(source.poll_once())  # uint32 wrap is forward time
        self.assertFalse(source.poll_once())  # old pre-wrap response
        self.assertTrue(source.poll_once())  # fresh boot, old rows cleared
        self.assertEqual(source.snapshot()["telemetry"]["wifi"], [])
        self.assertFalse(source.poll_once())  # old boot cannot restore old rows
        status = source.status()
        self.assertEqual(status["replayed_snapshots"], 2)
        self.assertEqual(status["boot_changes"], 1)
        self.assertEqual(status["boot_id"], "boot-b")

    def test_request_duration_is_retained_separately_from_radio_sample_age(self):
        clock = [100.0]

        class TimedClient(Client):
            def get_telemetry(self):
                clock[0] += 0.25
                return super().get_telemetry()

        source = self.source(TimedClient(snapshot()), clock)
        source.poll_once()
        clock[0] = 102.0
        result = source.snapshot()
        self.assertEqual(result["request_started_monotonic"], 100.0)
        self.assertEqual(result["received_monotonic"], 100.25)
        self.assertEqual(result["request_duration_s"], 0.25)
        self.assertEqual(result["observations"][0]["effective_age_ms"], 3750)
        self.assertIsNone(result["telemetry"]["timestamp_ms"])

    def test_failed_poll_retains_aging_snapshot_without_claiming_fresh_data(self):
        clock = [100.0]
        source = self.source(Client(snapshot(), CameraHTTPError(
            "camera_http_endpoint_unavailable", http_status=404)), clock)
        source.poll_once()
        clock[0] = 116.0
        self.assertFalse(source.poll_once())
        result = source.snapshot()
        self.assertFalse(result["fresh"])
        self.assertEqual(result["observations"][0]["effective_age_ms"], 18000)
        status = source.status()
        self.assertFalse(status["supported"])
        self.assertEqual(status["state"], "disconnected")
        self.assertNotIn("secret", json.dumps(status))
        self.assertEqual(source.client.calls, 2)

    def test_disabled_radio_does_not_poll_and_explicit_light_remains_available(self):
        client = Client()
        source = CameraTelemetrySource({"url": "/tmp/fixture.mp4"}, client=client)
        source.start()
        self.assertFalse(source.poll_once())
        self.assertIsNone(source.snapshot())
        self.assertEqual(source.status()["state"], "disabled")
        self.assertEqual(client.calls, 0)
        self.assertEqual(source.light_status(), light())
        self.assertEqual(source.set_light({"brightness": 0})["brightness"], 0)
        self.assertEqual(client.light_updates, [{"brightness": 0}])
        source.close()

    def test_snapshot_and_status_do_not_wait_for_slow_poll(self):
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()

        class BlockingClient(Client):
            def get_telemetry(self):
                entered.set()
                release.wait(2)
                return snapshot()

        source = self.source(BlockingClient(), poll_interval_s=30)
        source.start()
        try:
            self.assertTrue(entered.wait(1))

            def read_cache():
                self.assertIsNone(source.snapshot())
                self.assertEqual(source.status()["buffer"], "latest_snapshot_only")
                completed.set()

            reader = threading.Thread(target=read_cache)
            reader.start()
            self.assertTrue(completed.wait(1), "cache read blocked behind HTTP")
            reader.join()
            self.assertFalse(release.is_set())
        finally:
            release.set()
            source.close()


if __name__ == "__main__":
    unittest.main()
