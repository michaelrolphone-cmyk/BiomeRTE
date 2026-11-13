"""Authenticated camera HTTP access for light, AHRS, sonar and capture requests."""
from __future__ import annotations

import base64
import copy
import json
import math
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_RESPONSE_BYTES = 128 * 1024
MAX_LIGHT_BODY_BYTES = 256
UINT32_MAX = (1 << 32) - 1
MAC = re.compile(r"^(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _positive(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(name + " must be a finite positive number")
    return float(value)


def _integer(value, name, minimum=0, maximum=UINT32_MAX):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(name + " must be an integer in range")
    return value


def _text(value, name, maximum, empty=False):
    if not isinstance(value, str) or (not value and not empty) or "\x00" in value:
        raise ValueError(name + " must be text")
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(name + " is too long")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("nonfinite JSON number")


def _json_object(data):
    if not data or len(data) > MAX_RESPONSE_BYTES:
        raise ValueError("empty or oversized camera response")
    value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object,
                       parse_constant=_reject_constant)
    # This rejects numeric overflow and escaped unpaired surrogates in any
    # additive metadata, not only the fields this version knows.
    json.dumps(value, allow_nan=False, ensure_ascii=False).encode("utf-8")
    if not isinstance(value, dict):
        raise ValueError("camera response must be an object")
    return value


def validate_telemetry_snapshot(value):
    """Validate identity and timing without interpreting range or map evidence."""
    if not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1:
        raise ValueError("unsupported telemetry schema")
    _text(value.get("receiver_id"), "receiver_id", 128)
    _text(value.get("boot_id"), "boot_id", 64)
    for name in ("uptime_ms", "sequence"):
        _integer(value.get(name), name)
    if value.get("timestamp_ms") is not None:
        _integer(value["timestamp_ms"], "timestamp_ms", maximum=(1 << 64) - 1)
    return value


def encode_light_update(update):
    """Encode only a nonempty partial firmware light command before any I/O."""
    if not isinstance(update, dict) or not update:
        raise ValueError("light update must be a nonempty object")
    if set(update) - {"on", "brightness", "rgb"}:
        raise ValueError("only on, brightness, and rgb may be changed")
    if "on" in update and type(update["on"]) is not bool:
        raise ValueError("on must be a boolean")
    if "brightness" in update:
        _integer(update["brightness"], "brightness", maximum=255)
    if "rgb" in update:
        rgb = update["rgb"]
        if not isinstance(rgb, list) or len(rgb) != 3:
            raise ValueError("rgb must contain exactly three integers")
        for component in rgb:
            _integer(component, "rgb component", maximum=255)
    body = json.dumps(update, separators=(",", ":"), sort_keys=True,
                      allow_nan=False).encode("utf-8")
    if not 1 <= len(body) <= MAX_LIGHT_BODY_BYTES:
        raise ValueError("light update must be 1 to 256 bytes")
    return body


def _validate_light_state(value):
    for name in ("on", "ready", "command_state_known", "last_command_succeeded",
                 "hardware_verified", "optical_readback"):
        if type(value.get(name)) is not bool:
            raise ValueError("invalid light state")
    _integer(value.get("brightness"), "brightness", maximum=255)
    for name in ("rgb", "last_transmitted_rgb"):
        if not isinstance(value.get(name), list) or len(value[name]) != 3:
            raise ValueError("invalid light state")
        for component in value[name]:
            _integer(component, name, maximum=255)
    for name in ("last_command_ms", "last_attempt_ms"):
        if value.get(name) is not None:
            _integer(value[name], name)
    return value


class CameraHTTPError(RuntimeError):
    """Safe transport error; retain JSON driver receipts and POST uncertainty."""
    def __init__(self, code, *, http_status=None, response=None, outcome_known=True):
        super().__init__(code)
        self.code = code
        self.http_status = http_status
        self.response = copy.deepcopy(response)
        self.outcome_known = bool(outcome_known)

    def as_dict(self):
        result = {"error": self.code, "http_status": self.http_status,
                  "outcome_known": self.outcome_known}
        if self.response is not None:
            result["camera_response"] = copy.deepcopy(self.response)
        if not self.outcome_known:
            result["command_state_known"] = False
        return result


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        # Do not forward camera credentials or repeat a POST at another URL.
        return None


class CameraHTTPClient:
    """One request per call, no implicit mutation retry or redirect.

    base_url is an HTTP(S) origin. If absent it is derived from camera.url:
    an RTSP source uses HTTP on port 80, never the RTSP port. Authentication
    uses the same camera URL credentials or explicit camera username/password.
    An unprotected camera may omit credentials; HTTP 401 reports missing auth.
    """
    def __init__(self, camera_config, config=None, *, opener=None):
        self._camera = copy.deepcopy(camera_config)
        self._config = dict(config or {})
        self.network_timeout_s = _positive(self._config.get("network_timeout_s", 2.0),
                                           "network_timeout_s")
        if opener is None:
            opener = build_opener(ProxyHandler({}), _NoRedirect())
        self._open = opener.open if hasattr(opener, "open") else opener

    def _connection(self):
        # Resolve lazily: disabled telemetry must not break file/test video
        # sources that do not have an HTTP endpoint.
        try:
            source = urlsplit(self._camera.get("url", ""))
            configured = self._config.get("base_url")
            if configured:
                base = urlsplit(configured)
                if base.username is not None or base.password is not None:
                    raise ValueError("credentials belong to camera configuration")
                if source.hostname and base.hostname != source.hostname:
                    raise ValueError("HTTP origin differs from camera host")
            else:
                if source.scheme not in ("rtsp", "rtsps", "http", "https") or not source.hostname:
                    raise ValueError("camera source has no HTTP origin")
                scheme = source.scheme if source.scheme in ("http", "https") else "http"
                host = source.hostname
                if ":" in host:
                    host = "[" + host + "]"
                if source.scheme in ("http", "https") and source.port is not None:
                    host += ":" + str(source.port)
                base = urlsplit(urlunsplit((scheme, host, "", "", "")))
            if (base.scheme not in ("http", "https") or not base.hostname
                    or base.path not in ("", "/") or base.query or base.fragment):
                raise ValueError("base_url must be an HTTP origin")
            # Force port validation even when an explicit origin is supplied.
            _ = base.port
            username = self._camera.get("username")
            password = self._camera.get("password")
            if username is None:
                username = unquote(source.username) if source.username is not None else ""
            if password is None:
                password = unquote(source.password) if source.password is not None else ""
            if not isinstance(username, str) or not isinstance(password, str):
                raise ValueError("invalid camera credentials")
            if "\x00" in username or "\x00" in password or ":" in username:
                raise ValueError("invalid camera credentials")
            headers = {"Accept": "application/json", "Cache-Control": "no-cache"}
            if username:
                credentials = (username + ":" + password).encode("utf-8")
                headers["Authorization"] = "Basic " + base64.b64encode(credentials).decode("ascii")
            return urlunsplit((base.scheme, base.netloc, "", "", "")), headers
        except (ValueError, TypeError, UnicodeError):
            raise CameraHTTPError("camera_http_configuration_invalid") from None

    def _request(self, path, *, body=None, success_statuses=(200,), known_rejections=()):
        origin, headers = self._connection()
        mutation = body is not None
        if mutation:
            headers["Content-Type"] = "application/json"
        request = Request(origin + path, data=body, headers=headers,
                          method="POST" if mutation else "GET")
        status, payload = None, None
        try:
            try:
                response = self._open(request, timeout=self.network_timeout_s)
            except HTTPError as error:
                response = error  # A 503 JSON body is still a driver receipt.
            with response:
                status = response.getcode()
                data = response.read(MAX_RESPONSE_BYTES + 1)
            try:
                payload = _json_object(data)
            except (ValueError, UnicodeError, RecursionError):
                if status in success_statuses:
                    raise CameraHTTPError("camera_http_invalid_response", http_status=status,
                                          outcome_known=not mutation) from None
            if status not in success_statuses:
                code = {401: "camera_http_authentication_required",
                        403: "camera_http_forbidden",
                        404: "camera_http_endpoint_unavailable"}.get(status, "camera_http_error")
                # These firmware rejection statuses happen before hardware apply.
                known = not mutation or status in (400, 401, 403, 404, 413, 415) + tuple(known_rejections)
                raise CameraHTTPError(code, http_status=status, response=payload, outcome_known=known)
            return payload
        except CameraHTTPError:
            raise
        except (OSError, URLError, TimeoutError, HTTPException):
            # Never expose URLs, Authorization, passwords, or opaque socket text.
            raise CameraHTTPError("camera_http_transport_failed", http_status=status,
                                  outcome_known=not mutation) from None

    def get_telemetry(self):
        value = self._request("/api/telemetry")
        try:
            return validate_telemetry_snapshot(value)
        except (ValueError, UnicodeError):
            raise CameraHTTPError("camera_telemetry_invalid_response", http_status=200) from None

    def get_ultrasonic(self):
        """Read the four-channel firmware envelope; no sensor triggering occurs here."""
        return self._request("/api/ultrasonic")

    def get_ahrs(self):
        """Read full AHRS JSON; the AHRS source validates each sensor separately."""
        return self._request("/api/ahrs")

    def calibrate_gyro(self):
        """Queue once. HTTP 202 is acceptance, never calibration completion."""
        try:
            value = self._request("/api/ahrs/calibrate", body=b"{}",
                                  success_statuses=(202,), known_rejections=(409, 503))
        except CameraHTTPError as error:
            code = {409: "camera_ahrs_calibration_busy",
                    503: "camera_ahrs_calibration_unavailable"}.get(error.http_status)
            if code:
                raise CameraHTTPError(code, http_status=error.http_status,
                                      response=error.response,
                                      outcome_known=error.outcome_known) from None
            raise
        if value.get("status") != "queued":
            raise CameraHTTPError("camera_ahrs_calibration_invalid_response",
                                  http_status=202, response=value, outcome_known=False)
        return {"status": "queued", "accepted": True, "http_status": 202,
                "outcome_known": True, "completed": False,
                "completion_correlated": False, "camera_response": value}

    def get_light(self):
        value = self._request("/api/light")
        try:
            return _validate_light_state(value)
        except (ValueError, TypeError):
            raise CameraHTTPError("camera_light_invalid_response", http_status=200) from None

    def set_light(self, update):
        body = encode_light_update(update)
        value = self._request("/api/light", body=body)
        try:
            return _validate_light_state(value)
        except (ValueError, TypeError):
            raise CameraHTTPError("camera_light_invalid_response", http_status=200,
                                  response=value, outcome_known=False) from None



class SensorTelemetrySource:
    """One background HTTP poller with a bounded, latest-only snapshot buffer.

    snapshot() returns unmodified firmware data plus host provenance. Effective
    age adds elapsed host monotonic time to each firmware age. The request
    duration is retained separately as transport timing uncertainty; receipt
    time is never relabeled as the sensor observation time.
    """
    def __init__(self, camera_config, config=None, *, client=None,
                 monotonic=None, utc_clock=None):
        self._config = dict(config or {})
        self.enabled = self._config.get("enabled", False)
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        self.source_id = str(camera_config.get("source_id", "esp32-camera"))
        self.poll_interval_s = _positive(self._config.get("poll_interval_s", 5.0),
                                         "poll_interval_s")
        self.freshness_threshold_s = _positive(self._config.get("freshness_threshold_s", 15.0),
                                               "freshness_threshold_s")
        self.client = client if client is not None else CameraHTTPClient(camera_config, self._config)
        self._monotonic = monotonic or time.monotonic
        self._utc_clock = utc_clock or _utc_now
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._latest = None
        self._retired_epochs = deque(maxlen=32)
        self._polls = 0
        self._accepted = 0
        self._duplicates = 0
        self._replays = 0
        self._reboots = 0
        self._last_error = None
        self._last_attempt = None
        self._connected = False
        self._supported = None

    def start(self):
        if self.enabled and self._thread is None and not self._stop.is_set():
            self._thread = threading.Thread(target=self._run, name="vision-camera-telemetry", daemon=True)
            self._thread.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.poll_interval_s)

    def _read_snapshot(self):
        raise NotImplementedError("A sensor source must select and validate its endpoint")

    def _observations(self, value, elapsed):
        raise NotImplementedError("A sensor source must preserve its measurement ages")

    def poll_once(self):
        """Perform one polling attempt; intended for the poller or diagnostics."""
        if not self.enabled or self._stop.is_set():
            return False
        started = self._monotonic()
        attempted_at = self._utc_clock()
        try:
            telemetry = self._read_snapshot()
            received = self._monotonic()
            if self._stop.is_set():
                return False
            record = {
                "source_id": self.source_id,
                "received_at": self._utc_clock(),
                "received_monotonic": received,
                "request_started_monotonic": started,
                "request_duration_s": max(0.0, received - started),
                "telemetry": copy.deepcopy(telemetry),
            }
            epoch = (telemetry["receiver_id"], telemetry["boot_id"])
            with self._lock:
                self._polls += 1
                self._last_attempt = attempted_at
                self._connected = True
                self._supported = True
                self._last_error = None
                if self._latest is not None:
                    previous = self._latest["telemetry"]
                    prior_epoch = (previous["receiver_id"], previous["boot_id"])
                    if epoch == prior_epoch:
                        delta = (telemetry["uptime_ms"] - previous["uptime_ms"]) & UINT32_MAX
                        if delta == 0:
                            self._duplicates += 1
                            return False
                        if delta >= (1 << 31):
                            self._replays += 1
                            self._last_error = {"error": "camera_telemetry_replayed_snapshot"}
                            return False
                    elif epoch in self._retired_epochs:
                        self._replays += 1
                        self._last_error = {"error": "camera_telemetry_retired_boot"}
                        return False
                    else:
                        self._retired_epochs.append(prior_epoch)
                        self._reboots += 1
                self._latest = record
                self._accepted += 1
            return True
        except Exception as error:
            if isinstance(error, CameraHTTPError):
                details = error.as_dict()
            else:
                details = {"error": "camera_telemetry_poll_failed:" + type(error).__name__}
            with self._lock:
                self._polls += 1
                self._last_attempt = attempted_at
                self._connected = False
                self._last_error = details
                if details.get("http_status") == 404:
                    self._supported = False
            return False

    def snapshot(self):
        """Copy cached data and advance ages; never perform or wait for HTTP."""
        with self._lock:
            latest = self._latest
        if latest is None:
            return None
        value = copy.deepcopy(latest)
        elapsed = max(0.0, self._monotonic() - value["received_monotonic"])
        value["elapsed_since_receive_s"] = elapsed
        value["fresh"] = elapsed <= self.freshness_threshold_s
        value["age_clock"] = "firmware_age_plus_host_monotonic_since_receive"
        value["observations"] = self._observations(value, elapsed)
        return value

    def status(self):
        with self._lock:
            latest = self._latest
            result = {
                "state": "disabled" if not self.enabled else ("connected" if self._connected else "disconnected"),
                "enabled": self.enabled, "configured_source": self.source_id,
                "supported": self._supported, "polls": self._polls,
                "snapshots_accepted": self._accepted, "duplicate_snapshots": self._duplicates,
                "replayed_snapshots": self._replays, "boot_changes": self._reboots,
                "last_attempt_at": self._last_attempt,
                "error": copy.deepcopy(self._last_error),
                "buffer": "latest_snapshot_only",
            }
        age = None if latest is None else max(0.0, self._monotonic() - latest["received_monotonic"])
        result["last_receive_age_s"] = age
        result["fresh"] = age is not None and age <= self.freshness_threshold_s
        if latest is not None:
            result["receiver_id"] = latest["telemetry"]["receiver_id"]
            result["boot_id"] = latest["telemetry"]["boot_id"]
        return result

    def close(self):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            # Interruptible inter-poll wait; only a current bounded HTTP request
            # can delay shutdown. No extra fixed sleep or whole-job deadline.
            self._thread.join(self.client.network_timeout_s)
