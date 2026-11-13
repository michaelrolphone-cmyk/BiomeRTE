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
    CameraHTTPClient, CameraHTTPError, MAX_LIGHT_BODY_BYTES,
    MAX_RESPONSE_BYTES, UINT32_MAX, _NoRedirect, encode_light_update,
    validate_telemetry_snapshot,
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






if __name__ == "__main__":
    unittest.main()
