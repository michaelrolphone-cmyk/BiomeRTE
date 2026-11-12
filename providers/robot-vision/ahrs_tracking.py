"""Qualified AHRS evidence beside visual observations, without changing pose.

HTTP carries snapshots rather than every IMU sample. This coordinator never
integrates acceleration or supplies a visual pose prior. Firmware attitude maps
MPU axes into a gravity-aligned initial-yaw reference; camera mounting and camera
exposure timing remain unmeasured. Pressure altitude is not ground clearance.
"""
from __future__ import annotations

import copy
import math
import threading

from camera_telemetry import CameraHTTPError


_FRESHNESS_MS = {"imu": 500.0, "attitude": 500.0,
                 "mag": 1000.0, "barometer": 2000.0}
_MAX_EVIDENCE_GAP_S = 0.5
_UINT32 = 1 << 32
_ATTITUDE_FRAME = "MPU_axes_to_gravity_aligned_initial_yaw"
_PRESSURE_REFERENCE = "ISA_standard_pressure_not_AGL"


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except (ValueError, OverflowError):
        return False


def _uint32(value):
    return not isinstance(value, bool) and isinstance(value, int) and 0 <= value < _UINT32


def _vector(value, size):
    if not isinstance(value, (list, tuple)) or len(value) != size:
        return None
    return [float(x) for x in value] if all(_number(x) for x in value) else None


def _quaternion(value):
    values = _vector(value, 4)
    if values is None:
        return None
    norm = math.hypot(*values)
    # Firmware rounds to five decimal places. Reject arbitrary nonunit inputs.
    if not math.isfinite(norm) or abs(norm - 1.0) > 0.001:
        return None
    return [x / norm for x in values]


def _empty_change(reason, *, pressure=False):
    value = {"available": False, "reason": reason, "interval_s": None,
             "previous_frame_id": None}
    if pressure:
        value.update(delta_m=None, reference=_PRESSURE_REFERENCE,
                     sea_level_reference_pa=101325, ground_clearance=False,
                     vertical_translation_verified=False, weather_compensated=False)
    else:
        value.update(angle_rad=None, angle_deg=None, camera_rotation_verified=False)
    return value


class AHRSTracking:
    """One pending observation and four accepted sensor endpoints, with no I/O in evidence."""

    def __init__(self, camera_config, config=None, *, source=None):
        self.config = copy.deepcopy(config or {})
        self.enabled = self.config.get("enabled", False)
        if not isinstance(self.enabled, bool):
            raise ValueError("ahrs.enabled must be boolean")
        if source is None:
            from ahrs_source import AHRSTelemetrySource
            source = AHRSTelemetrySource(camera_config, self.config)
        self.source = source
        self._lock = threading.RLock()
        self._context = (None, None, None)
        self._context_generation = 0
        self._previous = {}
        self._pending = None
        self._last_frame_key = None
        self._last_frame_tick = None
        self._stream = None
        self._break_reason = "no_accepted_previous_sample"
        self._error = None
        self._accepted_frames = 0
        self._last_evidence = None

    def _clear_continuity(self, reason):
        self._previous.clear()
        self._break_reason = reason

    def start(self):
        if self.enabled:
            try:
                self.source.start()
            except Exception as exc:
                with self._lock:
                    self._error = "ahrs_start_failed:" + type(exc).__name__
                    self._clear_continuity("source_error")
        return self

    def close(self):
        with self._lock:
            self._clear_continuity("closed")
            self._pending = None
        try:
            self.source.close()
        except Exception as exc:
            with self._lock:
                self._error = "ahrs_close_failed:" + type(exc).__name__

    def set_context(self, map_id=None, calibration_id=None, session_id=None):
        with self._lock:
            self._context = (map_id, calibration_id, session_id)
            self._context_generation += 1
            self._clear_continuity("context_changed")
            self._pending = self._last_frame_key = self._last_frame_tick = None
            self._last_evidence = self._stream = None

    def snapshot(self, at_monotonic=None):
        """Read the source cache; only an explicit calibration command performs I/O."""
        try:
            return self.source.snapshot(at_monotonic=at_monotonic)
        except Exception as exc:
            with self._lock:
                self._error = "ahrs_snapshot_failed:" + type(exc).__name__
                self._clear_continuity("source_error")
            return None

    def calibrate_gyro(self):
        # Queueing is not a calibration result. Invalidate pending endpoints even
        # on uncertain POST delivery; the source owns HTTP result/error semantics.
        with self._lock:
            self._clear_continuity("calibration_requested")
            self._pending = self._last_frame_key = None
        try:
            return self.source.calibrate_gyro()
        except CameraHTTPError:
            raise
        except Exception as exc:
            with self._lock:
                self._error = "ahrs_calibration_failed:" + type(exc).__name__
            raise CameraHTTPError(self._error, outcome_known=False) from exc

    def _base(self, frame, state):
        tick = frame.get("receive_decode_monotonic_s")
        return {
            "enabled": self.enabled, "available": False, "state": state,
            "source": "camera_ahrs",
            "map_id": self._context[0], "calibration_id": self._context[1],
            "session_id": self._context[2], "frame_id": frame.get("frame_id"),
            "receiver_id": None, "boot_id": None, "sensors": {},
            "calibration": {}, "attitude_reference": None,
            "sensor_rotation": _empty_change(state),
            "pressure_altitude_change": _empty_change(state, pressure=True),
            "timing": {
                "frame_receive_decode_monotonic_s": float(tick) if _number(tick) else None,
                "received_monotonic_s": None, "request_duration_s": None,
                "camera_capture_time_known": False, "timing_qualified": False,
                "physical_capture_time_uncertainty_s": None,
                "clock_qualification": "host_receive_decode_not_camera_exposure",
            },
            "visual_comparison": {
                "available": False, "reason": "mounting_and_capture_timing_unverified"},
            "position_verified": False, "navigation_usable": False,
            "heading_available": False, "camera_pose_corrected": False,
            "metric_scale_available": False, "motion_commands": False,
        }

    def _frame_key(self, frame):
        return (self._context_generation, frame.get("frame_id"),
                frame.get("receive_decode_monotonic_s"), frame.get("source_id"),
                frame.get("stream_epoch"), frame.get("calibration_id"))

    def _context_matches(self, frame, localization):
        if self._context[1] is not None and frame.get("calibration_id") != self._context[1]:
            return False
        if frame.get("session_id") is not None and frame["session_id"] != self._context[2]:
            return False
        if isinstance(localization, dict):
            if localization.get("frame_id", frame.get("frame_id")) != frame.get("frame_id"):
                return False
            if localization.get("map_id", self._context[0]) != self._context[0]:
                return False
        return True

    def _sensor(self, name, meta, active):
        meta = meta if isinstance(meta, dict) else {}
        data = meta.get("data") if isinstance(meta.get("data"), dict) else {}
        age = meta.get("age_ms")
        upper = meta.get("age_upper_bound_ms")
        valid = (active and meta.get("valid") is True and data.get("valid") is True
                 and _number(age) and 0 <= age <= _FRESHNESS_MS[name]
                 and _number(upper) and age <= upper <= _FRESHNESS_MS[name]
                 and _uint32(meta.get("sample_count"))
                 and _uint32(meta.get("observed_uptime_ms")))
        reason = "available" if valid else (
            "worker_not_active" if not active else str(meta.get("reason", "invalid_sensor")))
        if not valid and reason == "available":
            reason = "invalid_sensor_metadata"
        result = {
            "present": meta.get("present") is True, "valid": valid, "reason": reason,
            "age_ms": float(age) if _number(age) else None,
            "age_upper_bound_ms": float(upper) if _number(upper) else None,
            "sample_count": meta.get("sample_count") if _uint32(meta.get("sample_count")) else None,
            "observed_uptime_ms": meta.get("observed_uptime_ms")
            if _uint32(meta.get("observed_uptime_ms")) else None,
            "new_sample": False,
        }
        if not valid:
            return result
        if name == "imu":
            acceleration = _vector(data.get("accel_m_s2"), 3)
            gyro = _vector(data.get("gyro_rad_s"), 3)
            if (data.get("frame") != "mpu9250_native_xyz" or acceleration is None or gyro is None
                    or not math.isfinite(math.hypot(*acceleration))
                    or not math.isfinite(math.hypot(*gyro))):
                result.update(valid=False, reason="invalid_imu_vector_or_frame")
                return result
            result.update(frame="mpu9250_native_xyz", accel_m_s2=acceleration,
                          gyro_rad_s=gyro, accel_norm_m_s2=math.hypot(*acceleration),
                          gyro_norm_rad_s=math.hypot(*gyro),
                          acceleration_includes_gravity=True,
                          gyro_bias_applied=data.get("gyro_bias_applied") is True)
            raw = _vector(data.get("gyro_raw_rad_s"), 3)
            if raw is not None:
                result["gyro_raw_rad_s"] = raw
        elif name == "mag":
            field = _vector(data.get("field_uT"), 3)
            if data.get("frame") != "ak8963_native_xyz" or field is None:
                result.update(valid=False, reason="invalid_magnetic_vector_or_frame")
                return result
            result.update(frame="ak8963_native_xyz", field_uT=field,
                          calibration=data.get("calibration"), used_for_heading=False,
                          installation_compass_calibrated=False)
        elif name == "barometer":
            if not _number(data.get("pressure_pa")) or data["pressure_pa"] <= 0:
                result.update(valid=False, reason="invalid_pressure")
                return result
            altitude_valid = (_number(data.get("pressure_altitude_m"))
                              and data.get("sea_level_reference_pa") == 101325
                              and data.get("altitude_reference") == _PRESSURE_REFERENCE)
            result.update(pressure_pa=float(data["pressure_pa"]),
                          pressure_altitude_m=float(data["pressure_altitude_m"])
                          if altitude_valid else None,
                          altitude_reference=_PRESSURE_REFERENCE if altitude_valid else None,
                          sea_level_reference_pa=101325 if altitude_valid else None,
                          altitude_valid=altitude_valid, ground_clearance=False)
        elif name == "attitude":
            quaternion = _quaternion(data.get("quaternion_wxyz"))
            reference_valid = (data.get("reference") == "relative_yaw"
                               and data.get("frame") == _ATTITUDE_FRAME
                               and data.get("heading_absolute") is False
                               and data.get("mounting_alignment_applied") is False
                               and _uint32(data.get("generation")) and data["generation"] > 0
                               and _uint32(data.get("yaw_origin_uptime_ms")))
            if quaternion is None or not reference_valid:
                result.update(valid=False, reason="invalid_quaternion_or_attitude_reference")
                return result
            result.update(quaternion_wxyz=quaternion, reference="relative_yaw",
                          frame=_ATTITUDE_FRAME, generation=data["generation"],
                          yaw_origin_uptime_ms=data["yaw_origin_uptime_ms"],
                          heading_absolute=False, mounting_alignment_applied=False,
                          reset_reason=data.get("reset_reason"))
            for key in ("roll_deg", "pitch_deg", "yaw_deg"):
                result[key] = float(data[key]) if _number(data.get(key)) else None
        if _number(data.get("temp_c")):
            result["temp_c"] = float(data["temp_c"])
        return result

    def _candidate(self, name, sensor, envelope, frame):
        if not sensor.get("valid"):
            self._previous.pop(name, None)
            return None
        reference = (envelope["receiver_id"], envelope["boot_id"])
        if name == "attitude":
            reference += (sensor["generation"], sensor["yaw_origin_uptime_ms"])
        identity = reference + (sensor["sample_count"], sensor["observed_uptime_ms"])
        previous = self._previous.get(name)
        sensor["new_sample"] = previous is None or identity != previous["identity"]
        return {"identity": identity, "reference": reference,
                "sample_count": sensor["sample_count"],
                "observed_uptime_ms": sensor["observed_uptime_ms"],
                "frame_tick": frame.get("measurement_monotonic_s",frame["receive_decode_monotonic_s"]),
                "frame_id": frame["frame_id"], "data": copy.deepcopy(sensor)}

    def _change(self, name, current, *, pressure=False):
        result = _empty_change(self._break_reason, pressure=pressure)
        if current is None:
            result["reason"] = "sensor_unavailable"
            return result
        if pressure and not current["data"].get("altitude_valid"):
            result["reason"] = "pressure_altitude_reference_unavailable"
            return result
        previous = self._previous.get(name)
        if previous is None:
            return result
        if current["reference"] != previous["reference"]:
            result["reason"] = "sensor_reference_changed"
            return result
        if current["identity"] == previous["identity"]:
            result["reason"] = "reused_sample"
            return result
        elapsed = (current["observed_uptime_ms"] - previous["observed_uptime_ms"]) % _UINT32 / 1000.0
        count_advanced = (current["sample_count"] > previous["sample_count"]
                          or current["sample_count"] == previous["sample_count"] == _UINT32 - 1)
        host_elapsed = current["frame_tick"] - previous["frame_tick"]
        if not count_advanced or not 0 < elapsed:
            result["reason"] = "sample_order_invalid"
            return result
        if elapsed > _MAX_EVIDENCE_GAP_S or host_elapsed > _MAX_EVIDENCE_GAP_S:
            result["reason"] = "evidence_gap"
            return result
        if host_elapsed <= 0:
            result["reason"] = "frame_order_invalid"
            return result
        if pressure:
            if not previous["data"].get("altitude_valid"):
                result["reason"] = "pressure_altitude_reference_unavailable"
                return result
            delta = current["data"]["pressure_altitude_m"] - previous["data"]["pressure_altitude_m"]
            if not _number(delta):
                result["reason"] = "invalid_pressure_altitude_change"
                return result
            result["delta_m"] = delta
        else:
            dot = sum(a * b for a, b in zip(previous["data"]["quaternion_wxyz"],
                                           current["data"]["quaternion_wxyz"]))
            angle = 2.0 * math.acos(min(1.0, abs(dot)))
            result.update(angle_rad=angle, angle_deg=math.degrees(angle))
        result.update(available=True, reason="descriptive_sensor_change",
                      interval_s=elapsed, previous_frame_id=previous["frame_id"])
        return result

    def evidence(self, frame, localization=None):
        """Freeze causal evidence for one frame; record_pose() alone accepts endpoints."""
        frame = frame if isinstance(frame, dict) else {}
        with self._lock:
            if not self.enabled:
                return self._base(frame, "disabled")
            key = self._frame_key(frame)
            if (key == self._last_frame_key and self._last_evidence is not None
                    and self._context_matches(frame, localization)):
                return copy.deepcopy(self._last_evidence)
            tick = frame.get("measurement_monotonic_s",frame.get("receive_decode_monotonic_s"))
            if (not _number(tick) or not isinstance(frame.get("frame_id"), str)
                    or not frame["frame_id"]):
                self._clear_continuity("invalid_frame")
                self._pending = self._last_frame_key = self._last_evidence = None
                return self._base(frame, "invalid_frame")
            if not self._context_matches(frame, localization):
                self._clear_continuity("context_mismatch")
                self._pending = self._last_frame_key = self._last_evidence = None
                return self._base(frame, "context_mismatch")
            stream = (frame.get("source_id"), frame.get("stream_epoch"))
            reason = None
            if self._pending is not None and not self._pending["accepted"]:
                reason = "previous_observation_not_accepted"
            if self._stream is not None and stream != self._stream:
                reason = "stream_changed"
            if self._last_frame_tick is not None:
                if tick <= self._last_frame_tick:
                    reason = "frame_order_invalid"
                elif tick - self._last_frame_tick > _MAX_EVIDENCE_GAP_S:
                    reason = "evidence_gap"
            if reason:
                self._clear_continuity(reason)
            self._last_frame_key, self._last_frame_tick, self._stream = key, tick, stream
            if frame.get('sensor_packet') is not None:
                from capture_packet import frame_ahrs_snapshot
                envelope=frame_ahrs_snapshot(frame)
            else:
                envelope = self.snapshot(at_monotonic=float(tick))
            value = self._base(frame, "telemetry_unavailable")
            candidates = {}
            if isinstance(envelope, dict):
                received = envelope.get("received_monotonic")
                duration = envelope.get("request_duration_s")
                if (_number(received) and received <= tick
                        and _number(duration) and duration >= 0
                        and isinstance(envelope.get("receiver_id"), str) and envelope["receiver_id"]
                        and isinstance(envelope.get("boot_id"), str) and envelope["boot_id"]
                        and envelope.get("available") is True):
                    telemetry = envelope.get("telemetry")
                    payload = telemetry.get("ahrs", {}) if isinstance(telemetry, dict) else {}
                    payload = payload if isinstance(payload, dict) else {}
                    active = payload.get("worker_ready") is True and payload.get("paused") is False
                    value.update(receiver_id=envelope["receiver_id"], boot_id=envelope["boot_id"])
                    value["timing"].update(received_monotonic_s=float(received),
                                           request_duration_s=float(duration),
                                           source_received_at=envelope.get("received_at"))
                    if envelope.get('device_frame_aligned'):
                        value['timing'].update(camera_capture_time_known=True,timing_qualified=True,
                            clock_qualification='same_device_capture_clock',
                            capture_monotonic_us=envelope['capture_monotonic_us'],
                            physical_capture_time_uncertainty_s=None)
                    metadata = envelope.get("sensors")
                    metadata = metadata if isinstance(metadata, dict) else {}
                    for name in ("imu", "mag", "barometer", "attitude"):
                        sensor = self._sensor(name, metadata.get(name), active)
                        if name == "attitude" and not value["sensors"]["imu"]["valid"]:
                            sensor.update(valid=False, reason="imu_unavailable")
                            for field in ("quaternion_wxyz", "roll_deg", "pitch_deg", "yaw_deg"):
                                sensor.pop(field, None)
                        value["sensors"][name] = sensor
                        candidates[name] = self._candidate(name, sensor, envelope, frame)
                    count = sum(s["valid"] for s in value["sensors"].values())
                    value.update(available=count > 0,
                                 state="available" if count == 4 else "partial" if count else "sensors_unavailable")
                    attitude = value["sensors"]["attitude"]
                    if attitude["valid"]:
                        value["attitude_reference"] = {
                            field: attitude[field] for field in
                            ("reference", "frame", "generation", "yaw_origin_uptime_ms", "reset_reason")}
                    calibration = envelope.get("calibration")
                    if isinstance(calibration, dict) and calibration.get("valid") is True:
                        data = calibration.get("data")
                        if isinstance(data, dict):
                            value["calibration"] = {
                                "status": data.get("status"),
                                "gyro_bias_valid": data.get("gyro_bias_valid") is True,
                                "result_from_snapshot": True,
                            }
                    value["sensor_rotation"] = self._change("attitude", candidates.get("attitude"))
                    value["pressure_altitude_change"] = self._change(
                        "barometer", candidates.get("barometer"), pressure=True)
                else:
                    value["state"] = "source_timing_or_identity_invalid"
            if not candidates:
                self._clear_continuity(value["state"])
                value["sensor_rotation"] = _empty_change(value["state"])
                value["pressure_altitude_change"] = _empty_change(value["state"], pressure=True)
            self._pending = {"key": key, "candidates": candidates, "accepted": False}
            self._last_evidence = copy.deepcopy(value)
            return value

    def record_pose(self, frame, localization=None):
        """Accept cached endpoints only after this exact observation is published."""
        if not self.enabled or not isinstance(frame, dict):
            return False
        with self._lock:
            pending = self._pending
            if (pending is None or pending["accepted"] or pending["key"] != self._frame_key(frame)
                    or not self._context_matches(frame, localization)):
                return False
            for name, candidate in pending["candidates"].items():
                if candidate is None:
                    self._previous.pop(name, None)
                    continue
                previous = self._previous.get(name)
                if previous is None or candidate["identity"] != previous["identity"]:
                    self._previous[name] = copy.deepcopy(candidate)
            pending["accepted"] = True
            self._accepted_frames += 1
            self._break_reason = "no_accepted_previous_sample"
            return True

    def status(self):
        try:
            source_status = self.source.status()
        except Exception as exc:
            source_status = {"state": "unavailable", "error": "ahrs_status_failed:" + type(exc).__name__}
        with self._lock:
            return {
                "enabled": self.enabled, "source": copy.deepcopy(source_status),
                "error": self._error, "map_id": self._context[0],
                "calibration_id": self._context[1], "session_id": self._context[2],
                "state": "disabled" if not self.enabled else
                self._last_evidence["state"] if self._last_evidence else "unobserved",
                "accepted_observations": self._accepted_frames,
                "accepted_sensor_endpoints": len(self._previous),
                "pending_observation": bool(self._pending and not self._pending["accepted"]),
                "max_evidence_gap_s": _MAX_EVIDENCE_GAP_S,
                "position_verified": False, "navigation_usable": False,
                "heading_available": False, "camera_pose_corrected": False,
                "metric_scale_available": False, "motion_commands": False,
                "physical_capture_time_uncertainty_s": None,
            }

