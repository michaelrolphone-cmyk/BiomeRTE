"""Optional AHRS HTTP cache with causal, per-sensor observation provenance.

HTTP receipts are not camera exposure timestamps. Observation time is estimated
from the request midpoint and firmware age; the full RTT is retained, and no
clock or camera/IMU timing qualification is implied. Raw firmware data is never
rewritten into robot axes, absolute heading, position, or floor height.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import threading
import time
from collections import deque

from camera_telemetry import (
    CameraHTTPClient, CameraHTTPError, MAX_RESPONSE_BYTES, UINT32_MAX,
    _integer, _positive, _text, _utc_now,
)

FRESHNESS_MS = {"imu": 500, "attitude": 500, "mag": 1000, "barometer": 2000}
CALIBRATION_STATES = {
    "uncalibrated", "queued", "collecting", "succeeded", "motion_detected",
    "sampling_gap", "interrupted", "unavailable",
}


def _finite(value, name):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(name + " must be finite")
    return value


def _boolean(value, name):
    if type(value) is not bool:
        raise ValueError(name + " must be boolean")
    return value


def _optional_int(value, name, maximum=UINT32_MAX):
    return None if value is None else _integer(value, name, maximum=maximum)


def _vector(value, size, name, required):
    if value is None and not required:
        return
    if not isinstance(value, list) or len(value) != size:
        raise ValueError(name + " has invalid dimensions")
    for component in value:
        _finite(component, name)


def _number(value, name, required):
    if value is not None or required:
        _finite(value, name)


def validate_ahrs_snapshot(value):
    """Validate the bounded envelope; malformed sensors are isolated later."""
    if not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1:
        raise ValueError("unsupported AHRS schema")
    if value.get("type") != "ahrs":
        raise ValueError("invalid AHRS type")
    _text(value.get("receiver_id"), "receiver_id", 128)
    _text(value.get("boot_id"), "boot_id", 64)
    for name in ("uptime_ms", "sequence"):
        _integer(value.get(name), name)
    _optional_int(value.get("timestamp_ms"), "timestamp_ms", (1 << 64) - 1)
    state = value.get("ahrs")
    if not isinstance(state, dict):
        raise ValueError("AHRS state must be an object")
    _boolean(state.get("worker_ready"), "worker_ready")
    _boolean(state.get("paused"), "paused")
    # HTTP already enforces strict JSON and this byte bound. Repeat both for
    # injected clients so every accepted raw payload remains JSON serializable.
    # Malformed values that are valid JSON remain isolated at sensor boundaries.
    if len(json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ValueError("oversized AHRS snapshot")
    return value


def _health(row):
    for name in ("present", "valid"):
        _boolean(row.get(name), name)
    age = _optional_int(row.get("age_ms"), "age_ms")
    observed = _optional_int(row.get("observed_uptime_ms"), "observed_uptime_ms")
    _integer(row.get("sample_count"), "sample_count")
    if (age is None) != (observed is None):
        raise ValueError("incomplete sample provenance")
    if row["valid"] and (not row["present"] or age is None):
        raise ValueError("valid sensor has no sample")
    for name in ("address", "who_am_i"):
        _optional_int(row.get(name), name, 255)
    _integer(row.get("i2c_error"), "i2c_error", minimum=-(1 << 31), maximum=(1 << 31) - 1)
    _integer(row.get("error_count"), "error_count")
    if row.get("error") is not None:
        _text(row["error"], "error", 128)
    return age, observed, row["sample_count"]


def _sample_id(source_id, telemetry, sensor, count, observed, row):
    if observed is None:
        return None
    identity = [source_id, telemetry["receiver_id"], telemetry["boot_id"], sensor, count, observed]
    if sensor == "attitude":
        identity += [row["generation"], row["yaw_origin_uptime_ms"]]
    raw = json.dumps(identity, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return "camera-ahrs:" + hashlib.sha256(raw).hexdigest()


def _sensor(source_id, telemetry, name, received, duration):
    state = telemetry["ahrs"]
    row = state.get(name)
    result = {
        "present": False, "valid": False, "reason": "malformed_sensor",
        "structurally_valid": False, "age_ms": None, "effective_age_ms": None,
        "reported_age_ms": None, "age_upper_bound_ms": None,
        "freshness_threshold_ms": FRESHNESS_MS[name],
        "sample_count": None, "observed_uptime_ms": None,
        "observed_monotonic_s": None, "time_uncertainty_s": duration / 2.0,
        "sample_identity": None, "repeat_sample": False,
        "timing_qualified": False, "data": copy.deepcopy(row),
    }
    try:
        if not isinstance(row, dict):
            raise ValueError("missing sensor")
        valid = _boolean(row.get("valid"), "valid")
        if name == "attitude":
            age = _optional_int(row.get("age_ms"), "age_ms")
            _integer(row.get("generation"), "generation")
            _optional_int(row.get("yaw_origin_uptime_ms"), "yaw_origin_uptime_ms")
            _text(row.get("reset_reason"), "reset_reason", 128)
            if (row.get("reference") != "relative_yaw"
                    or row.get("frame") != "MPU_axes_to_gravity_aligned_initial_yaw"
                    or row.get("heading_absolute") is not False
                    or row.get("mounting_alignment_applied") is not False):
                raise ValueError("unsupported attitude frame")
            _vector(row.get("quaternion_wxyz"), 4, "quaternion_wxyz", valid)
            if row.get("quaternion_wxyz") is not None:
                norm = math.sqrt(sum(v * v for v in row["quaternion_wxyz"]))
                if not math.isfinite(norm) or abs(norm - 1.0) > 0.01:
                    raise ValueError("quaternion is not unit length")
            for key in ("roll_deg", "pitch_deg", "yaw_deg"):
                _number(row.get(key), key, valid)
            if valid and (age is None or not row["generation"] or row["yaw_origin_uptime_ms"] is None):
                raise ValueError("missing attitude epoch")
            imu = state.get("imu")
            if not isinstance(imu, dict):
                raise ValueError("missing IMU provenance")
            _, observed, count = _health(imu)
            present = imu["present"]
            if valid and observed is None:
                raise ValueError("missing attitude sample")
            result["sample_provenance"] = "imu_sample_with_attitude_generation"
        else:
            age, observed, count = _health(row)
            present = row["present"]
            if name == "imu":
                if row.get("frame") != "mpu9250_native_xyz":
                    raise ValueError("unsupported IMU frame")
                _boolean(row.get("gyro_bias_applied"), "gyro_bias_applied")
                for key in ("accel_m_s2", "gyro_rad_s", "gyro_raw_rad_s"):
                    _vector(row.get(key), 3, key, valid)
                _number(row.get("temp_c"), "temp_c", valid)
            elif name == "mag":
                if row.get("frame") != "ak8963_native_xyz" or row.get("used_for_heading") is not False:
                    raise ValueError("unsupported magnetic frame")
                _vector(row.get("field_uT"), 3, "field_uT", valid)
                _text(row.get("calibration"), "mag calibration", 64)
            else:
                for key in ("pressure_pa", "temp_c", "pressure_altitude_m"):
                    _number(row.get(key), key, valid)
                if row.get("pressure_pa") is not None and row["pressure_pa"] <= 0:
                    raise ValueError("nonpositive pressure")
                if (row.get("altitude_reference") != "ISA_standard_pressure_not_AGL"
                        or type(row.get("sea_level_reference_pa")) not in (int, float)
                        or row["sea_level_reference_pa"] != 101325):
                    raise ValueError("unsupported pressure reference")
        result.update(
            present=present, structurally_valid=True, reported_age_ms=age,
            age_ms=age, effective_age_ms=age, sample_count=count,
            observed_uptime_ms=observed,
            sample_identity=_sample_id(source_id, telemetry, name, count, observed, row),
            age_upper_bound_ms=None if age is None else age + duration * 1000.0,
            observed_monotonic_s=None if age is None else received - age / 1000.0 - duration / 2.0,
        )
        if not state["worker_ready"]:
            result["reason"] = "worker_not_ready"
        elif state["paused"]:
            result["reason"] = "paused"
        elif not valid:
            result["reason"] = "firmware_invalid"
        elif age is None:
            result["reason"] = "no_sample"
        else:
            result.update(valid=True, reason=None)
    except (ValueError, TypeError, OverflowError, UnicodeError, KeyError):
        pass
    return result


def _calibration(state):
    row = state.get("calibration")
    result = {"valid": False, "reason": "malformed_calibration", "data": copy.deepcopy(row)}
    try:
        if not isinstance(row, dict) or row.get("status") not in CALIBRATION_STATES:
            return result
        _boolean(row.get("gyro_bias_valid"), "gyro_bias_valid")
        _vector(row.get("gyro_bias_rad_s"), 3, "gyro_bias_rad_s", True)
        _integer(row.get("samples"), "samples")
        if type(row.get("window_ms")) is not int or row["window_ms"] != 2000 or row.get("persistent") is not False:
            return result
        result.update(valid=True, reason=None)
    except (ValueError, TypeError, OverflowError):
        pass
    return result


def _age_sensor(sensor, elapsed, duration):
    if sensor["effective_age_ms"] is not None:
        sensor["effective_age_ms"] += elapsed * 1000.0
        sensor["age_ms"] = sensor["effective_age_ms"]
        sensor["age_upper_bound_ms"] = sensor["effective_age_ms"] + max(duration, 2.0 * sensor["time_uncertainty_s"]) * 1000.0
        if sensor["valid"] and sensor["age_upper_bound_ms"] > sensor["freshness_threshold_ms"]:
            sensor.update(valid=False, reason="stale")
    return sensor


class AHRSTelemetrySource:
    """One independent bounded poller. Cache reads never wait for network I/O."""
    def __init__(self, camera_config, config=None, *, client=None, monotonic=None, utc_clock=None):
        self._config = dict(config or {})
        self.enabled = _boolean(self._config.get("enabled", False), "enabled")
        self.source_id = _text(camera_config.get("source_id", "esp32-camera"), "source_id", 128)
        self.poll_interval_s = _positive(self._config.get("poll_interval_s", 0.1), "poll_interval_s")
        self.history_capacity = _integer(self._config.get("history_capacity", 256), "history_capacity", 1, 4096)
        self.history_retention_s = _positive(self._config.get("history_retention_s", 30.0), "history_retention_s")
        if self.history_retention_s > 300:
            raise ValueError("history_retention_s must be at most 300")
        self.client = client if client is not None else CameraHTTPClient(camera_config, self._config)
        self._monotonic = monotonic or time.monotonic
        self._utc_clock = utc_clock or _utc_now
        self._lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._calibration_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._history = deque(maxlen=self.history_capacity)
        self._latest = None
        self._sensor_latest = {}
        self._retired_epochs = deque(maxlen=32)
        self._polls = self._accepted = self._duplicates = self._replays = self._reboots = 0
        self._connected = False
        self._supported = None
        self._last_error = self._last_attempt = self._calibration_request = None

    def start(self):
        if self.enabled and self._thread is None and not self._stop.is_set():
            self._thread = threading.Thread(target=self._run, name="vision-camera-ahrs", daemon=True)
            self._thread.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.poll_interval_s)

    def _prune(self, now):
        cutoff = now - self.history_retention_s
        while self._history and self._history[0]["received_monotonic"] < cutoff:
            self._history.popleft()

    def _merge_provenance(self, record):
        """Keep repeated samples aging; do not mistake HTTP republication for data."""
        for name, sensor in record["sensors"].items():
            if not sensor["structurally_valid"] or sensor["sample_identity"] is None:
                continue
            previous = self._sensor_latest.get(name)
            if previous:
                prior, at = previous
                count, old_count = sensor["sample_count"], prior["sample_count"]
                delta = (sensor["observed_uptime_ms"] - prior["observed_uptime_ms"]) & UINT32_MAX
                # Firmware counters saturate, while uptime wraps. A saturated
                # counter still identifies new samples through observed uptime.
                invalid = (count < old_count or delta >= (1 << 31)
                           or (count == old_count and count != UINT32_MAX and delta != 0))
                if name == "attitude":
                    new, old = sensor["data"], prior["data"]
                    generation_delta = (new["generation"] - old["generation"]) & UINT32_MAX
                    invalid = invalid or generation_delta >= (1 << 31)
                    invalid = invalid or (generation_delta == 0 and
                                          new["yaw_origin_uptime_ms"] != old["yaw_origin_uptime_ms"])
                if invalid:
                    sensor.update(valid=False, structurally_valid=False, reason="replayed_sample_provenance")
                    continue
                if sensor["sample_identity"] == prior["sample_identity"]:
                    sensor["repeat_sample"] = True
                    if prior["effective_age_ms"] is not None and sensor["effective_age_ms"] is not None:
                        sensor["effective_age_ms"] = max(
                            sensor["effective_age_ms"],
                            prior["effective_age_ms"] + max(0.0, record["received_monotonic"] - at) * 1000.0)
                        sensor["age_ms"] = sensor["effective_age_ms"]
                        sensor["observed_monotonic_s"] = min(
                            sensor["observed_monotonic_s"], prior["observed_monotonic_s"])
                        sensor["time_uncertainty_s"] = max(
                            sensor["time_uncertainty_s"], prior["time_uncertainty_s"])
            self._sensor_latest[name] = (copy.deepcopy(sensor), record["received_monotonic"])
        imu = record["sensors"]["imu"]
        attitude = record["sensors"]["attitude"]
        if not imu["valid"] and attitude["valid"]:
            attitude.update(valid=False, reason="imu_unavailable")

    def poll_once(self):
        if not self.enabled or self._stop.is_set() or not self._poll_lock.acquire(blocking=False):
            return False
        try:
            return self._poll_once()
        finally:
            self._poll_lock.release()

    def _poll_once(self):
        started, attempted_at = self._monotonic(), self._utc_clock()
        try:
            telemetry = validate_ahrs_snapshot(self.client.get_ahrs())
            received = self._monotonic()
            if self._stop.is_set():
                return False
            duration = max(0.0, received - started)
            record = {
                "source_id": self.source_id, "received_at": self._utc_clock(),
                "received_monotonic": received, "request_started_monotonic": started,
                "request_duration_s": duration, "telemetry": copy.deepcopy(telemetry),
                "receiver_id": telemetry["receiver_id"], "boot_id": telemetry["boot_id"],
                "sequence": telemetry["sequence"],
                "sensors": {name: _sensor(self.source_id, telemetry, name, received, duration)
                            for name in FRESHNESS_MS},
                "calibration": _calibration(telemetry["ahrs"]),
            }
            epoch = (record["receiver_id"], record["boot_id"])
            with self._lock:
                self._polls += 1
                self._last_attempt, self._last_error = attempted_at, None
                self._connected, self._supported = True, True
                if self._latest is not None:
                    previous = self._latest["telemetry"]
                    prior_epoch = (previous["receiver_id"], previous["boot_id"])
                    if epoch == prior_epoch:
                        delta = (telemetry["uptime_ms"] - previous["uptime_ms"]) & UINT32_MAX
                        if delta >= (1 << 31):
                            self._replays += 1
                            self._last_error = {"error": "camera_ahrs_replayed_snapshot"}
                            return False
                        if delta == 0 and telemetry["ahrs"] == previous["ahrs"]:
                            self._duplicates += 1
                            return False
                    elif epoch in self._retired_epochs:
                        self._replays += 1
                        self._last_error = {"error": "camera_ahrs_retired_boot"}
                        return False
                    else:
                        self._retired_epochs.append(prior_epoch)
                        self._reboots += 1
                        self._sensor_latest.clear()
                self._merge_provenance(record)
                self._latest = record
                self._history.append(record)
                self._prune(received)
                self._accepted += 1
            return True
        except Exception as error:
            details = error.as_dict() if isinstance(error, CameraHTTPError) else {
                "error": "camera_ahrs_poll_failed:" + type(error).__name__}
            with self._lock:
                self._polls += 1
                self._last_attempt, self._last_error = attempted_at, details
                self._connected = False
                if details.get("http_status") == 404:
                    self._supported = False
            return False

    def snapshot(self, at_monotonic=None):
        """Return the last receipt at/before the frame tick, with ages at that tick."""
        now = self._monotonic()
        at = now if at_monotonic is None else _finite(at_monotonic, "at_monotonic")
        if at > now:
            return None  # Do not manufacture a future observation.
        with self._lock:
            self._prune(now)
            latest = next((r for r in reversed(self._history) if r["received_monotonic"] <= at), None)
        if latest is None:
            return None
        value = copy.deepcopy(latest)
        elapsed = max(0.0, at - value["received_monotonic"])
        for sensor in value["sensors"].values():
            _age_sensor(sensor, elapsed, value["request_duration_s"])
        if not value["sensors"]["imu"]["valid"] and value["sensors"]["attitude"]["valid"]:
            value["sensors"]["attitude"].update(valid=False, reason="imu_unavailable")
        value.update(
            enabled=self.enabled, available=True,
            valid=any(s["valid"] for s in value["sensors"].values()),
            at_monotonic=at, elapsed_since_receive_s=elapsed,
            age_clock="firmware_age_plus_host_monotonic_since_receive",
            timing={"basis": "http_request_midpoint_minus_firmware_age",
                    "approximate": True, "timing_qualified": False,
                    "camera_exposure_time_bound": False,
                    "request_duration_s": value["request_duration_s"]},
        )
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
                "last_attempt_at": self._last_attempt, "error": copy.deepcopy(self._last_error),
                "buffer": "bounded_causal_history", "history_capacity": self.history_capacity,
                "history_retention_s": self.history_retention_s,
                "poll_interval_s": self.poll_interval_s,
                "calibration_request": copy.deepcopy(self._calibration_request),
            }
        snapshot = self.snapshot()
        # A poll may finish after the aggregate counters were copied above.
        # Observation identity and age must follow the exact sensor snapshot.
        observed = snapshot if snapshot is not None else latest
        result["last_receive_age_s"] = (
            snapshot["elapsed_since_receive_s"] if snapshot is not None else
            (None if observed is None else max(0.0, self._monotonic() - observed["received_monotonic"])))
        result["sensors"] = {} if snapshot is None else {
            name: {key: sensor[key] for key in (
                "present", "valid", "reason", "effective_age_ms", "age_upper_bound_ms", "sample_identity")}
            for name, sensor in snapshot["sensors"].items()}
        result["calibration"] = None if snapshot is None else snapshot["calibration"]
        if observed is not None:
            result.update(receiver_id=observed["receiver_id"], boot_id=observed["boot_id"])
        with self._lock:
            result["history_size"] = len(self._history)
        return result

    def calibrate_gyro(self):
        """Explicit one-shot request; subsequent telemetry cannot prove correlation."""
        if self._stop.is_set():
            raise CameraHTTPError("camera_ahrs_closed", outcome_known=True)
        if not self._calibration_lock.acquire(blocking=False):
            raise CameraHTTPError("camera_ahrs_calibration_busy", http_status=409, outcome_known=True)
        started, requested_at = self._monotonic(), self._utc_clock()
        try:
            try:
                receipt = self.client.calibrate_gyro()
                if not isinstance(receipt, dict) or receipt.get("accepted") is not True or receipt.get("status") != "queued":
                    raise CameraHTTPError("camera_ahrs_calibration_invalid_response", outcome_known=False)
                receipt = copy.deepcopy(receipt)
            except Exception as error:
                if not isinstance(error, CameraHTTPError):
                    error = CameraHTTPError("camera_ahrs_calibration_request_failed", outcome_known=False)
                receipt = error.as_dict()
                receipt.update(status="rejected" if error.outcome_known else "unknown",
                               accepted=False if error.outcome_known else None)
                self._record_calibration_request(receipt, started, requested_at)
                raise error from None
            return self._record_calibration_request(receipt, started, requested_at)
        finally:
            self._calibration_lock.release()

    def _record_calibration_request(self, receipt, started, requested_at):
        finished = self._monotonic()
        receipt.update(
            requested_at=requested_at, request_started_monotonic=started,
            request_finished_monotonic=finished, request_duration_s=max(0.0, finished - started),
            completed=False, completion_correlated=False,
            correlation="firmware_has_no_request_identifier",
        )
        with self._lock:
            self._calibration_request = copy.deepcopy(receipt)
        return receipt

    def close(self):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(getattr(self.client, "network_timeout_s", 2.0))
