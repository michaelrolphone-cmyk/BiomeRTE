"""Coherent, asynchronous radio evidence beside the visual pose estimator.

The firmware has no camera-frame capture timestamp. Alignment below uses host
decode/receive clocks and compensates each RF observation's actual age. This is
an explicitly approximate clock relationship, never a measured capture-time
error bound or a navigation permission.
"""
from __future__ import annotations

import copy
from collections import deque
from datetime import datetime, timezone
import math
import sqlite3
from pathlib import Path
import threading
import time

import numpy as np

from camera_telemetry import CameraTelemetrySource


def _number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _pose(value):
    try:
        result = np.asarray(value, dtype=float)
        if result.shape != (4, 4) or not np.isfinite(result).all():
            return None
        if not np.allclose(result[3], [0, 0, 0, 1], atol=1e-6):
            return None
        return result
    except (TypeError, ValueError):
        return None


def _unavailable(reason, map_id=None, calibration_id=None):
    return {
        "state": reason, "map_id": map_id, "calibration_id": calibration_id,
        "position": None, "candidate_keyframe_ids": [], "candidates": [], "clusters": [],
        "valid_visual_pose": False, "heading_available": False, "navigation_usable": False,
        "confidence": {"score": 0.0, "kind": "heuristic_not_probability"},
    }


class RadioTracking:
    """Pose history and model work never perform network or SQLite I/O in track()."""

    def __init__(self, camera_config, config, state_dir, *, source=None, model=None):
        self.config = copy.deepcopy(config or {})
        enabled = self.config.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("radio.enabled must be boolean")
        self.enabled = enabled
        self.receiver_calibration_id = self.config.get("receiver_calibration_id", "rssi-v1")
        if (not isinstance(self.receiver_calibration_id, str)
                or not self.receiver_calibration_id.strip() or len(self.receiver_calibration_id) > 128):
            raise ValueError("radio.receiver_calibration_id must be a nonempty string of at most 128 characters")
        self.mapping_config = copy.deepcopy(self.config.get("mapping", {}))
        if not isinstance(self.mapping_config, dict):
            raise ValueError("radio.mapping must be an object")
        self.max_source_age = self._positive("max_source_age_s", 15.0, mapping=True)
        self.max_span = self._positive("max_fingerprint_span_s", 1.0, mapping=True)
        self.max_alignment = self._positive("max_pose_time_delta_s", 0.5, mapping=True)
        self.prior_max_age = self._positive("prior_max_age_s", 5.0)
        self.history_seconds = self._positive("pose_history_s", 180.0)
        self.history_limit = self.config.get("pose_history_limit", 1200)
        if isinstance(self.history_limit, bool) or not isinstance(self.history_limit, int) or self.history_limit < 2:
            raise ValueError("radio.pose_history_limit must be an integer >= 2")
        self.source = source or CameraTelemetrySource(camera_config, self.config)
        self.model = model
        self._initialization_error = None
        if self.enabled and self.model is None:
            from radio_map import RadioMap, validate_config
            # Invalid configuration remains an explicit startup error. Storage
            # failure disables this optional model without taking down vision.
            validate_config(self.mapping_config)
            try:
                folder = Path(state_dir) / "radio"
                folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                self.model = RadioMap(folder / "radio.sqlite3", self.mapping_config)
            except (OSError, sqlite3.Error, ValueError) as exc:
                self._initialization_error = "radio_model_unavailable:" + type(exc).__name__
        self._condition = threading.Condition(threading.RLock())
        self._model_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._map_id = self._calibration_id = None
        self._map_revision = 0
        self._generation = self._anchor_version = 0
        self._anchors = {}
        self._poses = deque(maxlen=self.history_limit)
        self._stream_epoch = None
        self._estimate = _unavailable(self._idle_state())
        self._estimate_generation = self._estimate_anchor_version = -1
        self._sample_time = self._newest_sample_time = self._receive_time = None
        self._received_at = None
        self._last_training = {}
        self._model_status = {}
        self._error = self._initialization_error
        self._last_token = None
        self._pose_revision = 0
        self._last_pose_revision = -1
        self._alignment_pending = False

    def _idle_state(self):
        if not self.enabled:
            return "disabled"
        return "model_unavailable" if self._initialization_error else "unobserved"

    def _positive(self, name, default, *, mapping=False):
        value = (self.mapping_config if mapping else self.config).get(name, default)
        if not _number(value) or value <= 0:
            raise ValueError("radio." + name + " must be finite and positive")
        return float(value)

    def start(self):
        self.source.start()
        if self.enabled and self.model is not None and self._thread is None:
            self._thread = threading.Thread(target=self._run, name="vision-radio-model", daemon=True)
            self._thread.start()
        return self

    def set_context(self, map_id, calibration_id, keyframes, revision=0):
        with self._condition:
            self._map_id, self._calibration_id = map_id, calibration_id
            self._map_revision = revision
            self._anchors = copy.deepcopy(keyframes)
            self._poses.clear()
            self._stream_epoch = None
            self._generation += 1
            self._anchor_version += 1
            self._estimate = _unavailable(self._idle_state(), map_id, calibration_id)
            self._sample_time = self._newest_sample_time = self._receive_time = None
            self._received_at = None
            self._last_training = {}
            self._last_token = None
            self._pose_revision += 1
            self._last_pose_revision = -1
            self._alignment_pending = False
            self._condition.notify_all()

    def update_geometry(self, map_id, calibration_id, changes, revision):
        with self._condition:
            if (map_id, calibration_id) != (self._map_id, self._calibration_id):
                return
            changed = False
            for item in changes:
                if item.get("kind") != "keyframe":
                    continue
                identifier = str(item["entity_id"])
                if item.get("op") == "delete":
                    changed |= self._anchors.pop(identifier, None) is not None
                elif item.get("op") == "upsert":
                    data = copy.deepcopy(item.get("data", {}))
                    if self._anchors.get(identifier) != data:
                        self._anchors[identifier] = data
                        changed = True
            if changed:
                self._anchor_version += 1
            self._map_revision = revision
            self._condition.notify_all()

    def record_pose(self, frame, localization):
        """Record only a successfully published, independently verified visual pose."""
        if not self.enabled:
            return
        tick = frame.get("receive_decode_monotonic_s")
        if not _number(tick):
            return
        with self._condition:
            epoch = frame.get("stream_epoch")
            if self._stream_epoch != epoch:
                self._poses.clear()
                self._pose_revision += 1
                self._stream_epoch = epoch
            if (localization.get("valid") is not True
                    or localization.get("frame_id") != frame.get("frame_id")
                    or localization.get("map_id") != self._map_id
                    or frame.get("calibration_id") != self._calibration_id):
                return
            transform = _pose(localization.get("T_map_camera"))
            identifier = str(localization.get("reference_keyframe_id"))
            keyframe = self._anchors.get(identifier)
            if transform is None or not keyframe or not keyframe.get("source_frame_id"):
                return
            reference = _pose(keyframe.get("T_map_camera"))
            if reference is None:
                return
            try:
                relative = np.linalg.solve(reference, transform)
            except np.linalg.LinAlgError:
                return
            visual = {
                "valid": True, "source": "visual", "independently_validated": True,
                "map_id": self._map_id, "calibration_id": self._calibration_id,
                "timestamp_s": float(tick), "source_frame_id": frame["frame_id"],
                "T_map_camera": transform.tolist(),
                "clock_qualification": "host_decode_time_not_camera_capture_time",
            }
            anchor = {
                "keyframe_id": identifier, "T_keyframe_camera": relative.tolist(),
                "source_frame_id": keyframe["source_frame_id"],
                "backend_frame_index": keyframe.get("backend_frame_index"),
            }
            self._poses.append({"visual": visual, "anchor": anchor})
            self._pose_revision += 1
            while self._poses and tick - self._poses[0]["visual"]["timestamp_s"] > self.history_seconds:
                self._poses.popleft()
            self._condition.notify_all()

    def _groups(self, envelope, now_s):
        payload = envelope.get("telemetry")
        if not isinstance(payload, dict):
            return []
        received = envelope.get("received_monotonic")
        duration = envelope.get("request_duration_s", 0.0)
        if not _number(received) or not _number(duration) or duration < 0:
            return []
        rows = []
        for meta in envelope.get("observations", []):
            radio, index = meta.get("radio"), meta.get("index")
            if radio not in ("wifi", "ble") or isinstance(index, bool) or not isinstance(index, int):
                continue
            entries = payload.get(radio, [])
            if not isinstance(entries, list) or not 0 <= index < len(entries):
                continue
            original = entries[index]
            if not isinstance(original, dict):
                continue
            age = original.get("age_ms")
            if not _number(age) or age < 0:
                continue
            # No host-clock subtraction from unsynchronised firmware uptime.
            # RTT midpoint reduces transport bias; camera capture lag is still unknown.
            observed = float(received) - float(age) / 1000.0 - float(duration) / 2.0
            if not 0 <= now_s - observed <= self.max_source_age:
                continue
            rows.append({**copy.deepcopy(original), "radio": radio,
                         "observed_at_s": observed, "time_uncertainty_s": duration / 2.0,
                         "source_observation_id": meta.get("source_observation_id")})
        rows.sort(key=lambda row: row["observed_at_s"])
        groups = []
        for row in rows:
            if not groups or row["observed_at_s"] - groups[-1][0]["observed_at_s"] > self.max_span:
                groups.append([])
            groups[-1].append(row)
        return groups

    def evaluate_once(self, *, now_s=None, now_utc_s=None):
        """Deterministic worker step; public for replay qualification with fixtures."""
        if not self.enabled or self.model is None:
            return
        now_s = time.monotonic() if now_s is None else now_s
        now_utc_s = time.time() if now_utc_s is None else now_utc_s
        envelope = self.source.snapshot()
        with self._condition:
            context = (self._map_id, self._calibration_id, self._generation, self._anchor_version)
            anchors, poses = copy.deepcopy(self._anchors), list(self._poses)
            pose_revision = self._pose_revision
        map_id, calibration_id, generation, anchor_version = context
        if not map_id:
            return
        if not envelope or envelope.get("fresh") is not True:
            with self._condition:
                if generation == self._generation:
                    self._estimate = _unavailable("telemetry_unavailable_or_stale", map_id, calibration_id)
                    self._estimate_generation, self._estimate_anchor_version = generation, anchor_version
                    self._sample_time = self._newest_sample_time = None
            return
        groups = self._groups(envelope, now_s)
        payload = envelope["telemetry"]
        identity = {"receiver_id": payload.get("receiver_id"), "boot_id": payload.get("boot_id"),
                    "receiver_calibration_id": self.receiver_calibration_id}
        token = (generation, envelope.get("received_monotonic"), payload.get("boot_id"), payload.get("uptime_ms"))
        training = []
        with self._model_lock:
            # New HTTP delivery can contain old RF events; the persistent model
            # additionally deduplicates actual source observation identities.
            if (token != self._last_token
                    or (self._alignment_pending and pose_revision != self._last_pose_revision)):
                alignment_pending = False
                for group in groups:
                    center = (group[0]["observed_at_s"] + group[-1]["observed_at_s"]) / 2.0
                    eligible = [p for p in poses
                                if p["anchor"]["keyframe_id"] in anchors
                                and anchors[p["anchor"]["keyframe_id"]].get("source_frame_id") == p["anchor"].get("source_frame_id")]
                    nearest = min(eligible, key=lambda p: abs(p["visual"]["timestamp_s"] - center), default=None)
                    if nearest is None or any(
                            abs(row["observed_at_s"] - nearest["visual"]["timestamp_s"]) + row["time_uncertainty_s"] > self.max_alignment
                            for row in group):
                        alignment_pending = True
                        training.append({"accepted": False, "reason": "no_time_aligned_independent_visual_pose"})
                        continue
                    training.append(self.model.train(
                        map_id, calibration_id, {**identity, "observations": group},
                        nearest["visual"], nearest["anchor"], now_s=now_s, now_utc_s=now_utc_s))
                self._last_token = token
                self._last_pose_revision = pose_revision
                self._alignment_pending = alignment_pending
            minimum = self.mapping_config.get("min_sources", 3)
            usable = [group for group in groups if len(group) >= minimum]
            group = max(usable, key=lambda g: g[-1]["observed_at_s"], default=None)
            if group is None:
                estimate = _unavailable("insufficient_coherent_radio_sources", map_id, calibration_id)
                sample_time = newest_sample_time = None
            else:
                estimate = self.model.estimate(
                    map_id, calibration_id, {**identity, "observations": group},
                    anchors, now_s=now_s, now_utc_s=now_utc_s)
                sample_time = min(row["observed_at_s"] for row in group)
                newest_sample_time = max(row["observed_at_s"] for row in group)
            model_status = self.model.status()
        with self._condition:
            if generation != self._generation:
                return
            self._estimate = copy.deepcopy(estimate)
            self._estimate_generation, self._estimate_anchor_version = generation, anchor_version
            self._sample_time, self._newest_sample_time = sample_time, newest_sample_time
            self._receive_time = envelope.get("received_monotonic")
            self._received_at = envelope.get("received_at")
            self._model_status = model_status
            if training:
                self._last_training = {
                    "groups": len(training), "accepted": sum(x.get("accepted") is True for x in training),
                    "results": training[-8:], "clock_qualification": "approximate_host_receive_alignment",
                    "physical_capture_time_uncertainty_s": None,
                }
            self._error = None

    def _run(self):
        while not self._stop.is_set():
            try:
                self.evaluate_once()
            except Exception as exc:
                with self._condition:
                    self._error = "radio_model_failed:" + type(exc).__name__
                    self._estimate = _unavailable("model_error", self._map_id, self._calibration_id)
                    self._estimate_generation = self._generation
                    self._estimate_anchor_version = self._anchor_version
                    self._sample_time = self._newest_sample_time = None
            with self._condition:
                self._condition.wait_for(self._stop.is_set, timeout=0.5)

    def prediction(self, *, now_s=None):
        now_s = time.monotonic() if now_s is None else now_s
        with self._condition:
            value = copy.deepcopy(self._estimate)
            sample, newest, received = self._sample_time, self._newest_sample_time, self._receive_time
            if self._estimate_generation != self._generation:
                value = _unavailable(self._idle_state(), self._map_id, self._calibration_id)
            elif self._estimate_anchor_version != self._anchor_version:
                value = _unavailable("geometry_changed_pending_refresh", self._map_id, self._calibration_id)
            elif newest is not None and now_s < newest:
                value = _unavailable("radio_observations_after_frame", self._map_id, self._calibration_id)
            elif sample is not None and (now_s < sample or now_s - sample > self.max_source_age):
                value = _unavailable("radio_observations_stale", self._map_id, self._calibration_id)
            value.update(map_id=self._map_id, calibration_id=self._calibration_id,
                         map_revision=self._map_revision, units="arbitrary",
                         clock_qualification="approximate_host_receive_alignment",
                         physical_capture_time_uncertainty_s=None,
                         source_received_at=self._received_at,
                         observation_age_s=None if sample is None else max(0.0, now_s - sample),
                         observation_span_s=None if sample is None or newest is None else newest - sample,
                         valid_visual_pose=False, heading_available=False, navigation_usable=False)
            if sample is not None:
                value["expires_at_utc"] = datetime.fromtimestamp(
                    time.time() + sample + self.max_source_age - now_s, timezone.utc).isoformat()
            value["telemetry_receive_age_s"] = None if received is None else max(0.0, now_s - received)
            return value

    def prior(self, *, now_s=None):
        value = self.prediction(now_s=now_s)
        age = value.get("observation_age_s")
        if age is None or age > self.prior_max_age or value.get("state") in (
                "unobserved", "disabled", "geometry_changed_pending_refresh",
                "radio_observations_stale", "radio_observations_after_frame",
                "telemetry_unavailable_or_stale", "model_error", "model_unavailable"):
            return []
        with self._condition:
            available = set(self._anchors)
        result = []
        for identifier in value.get("candidate_keyframe_ids", []):
            key = str(identifier)
            if key not in available or key in result:
                continue
            # Native Frame.id is an integer, unlike arbitrary external source IDs.
            try:
                if int(key) < 0 or str(int(key)) != key:
                    continue
            except (TypeError, ValueError):
                continue
            result.append(key)
            if len(result) >= self.mapping_config.get("max_candidates", 12):
                break
        return result

    def evidence(self, localization, *, now_s=None):
        value = self.prediction(now_s=now_s)
        comparison = {"state": "unavailable"}
        visual = _pose(localization.get("T_map_camera")) if localization.get("valid") is True else None
        position = value.get("position")
        if visual is not None and isinstance(position, (list, tuple)) and len(position) == 3:
            try:
                separation = float(np.linalg.norm(visual[:3, 3] - np.asarray(position, dtype=float)))
                tolerance = float(self.mapping_config.get("cluster_radius_map_units", 1.0))
                if math.isfinite(separation):
                    comparison = {"state": "consistent" if separation <= tolerance else "disagreement",
                                  "separation_map_units": separation, "tolerance_map_units": tolerance,
                                  "meaning": "heuristic_support_not_visual_pose_acceptance"}
            except (TypeError, ValueError):
                pass
        elif value.get("state") == "ambiguous":
            comparison = {"state": "ambiguous_radio_modes"}
        keep = ("state", "map_id", "calibration_id", "map_revision", "position", "units", "confidence",
                "candidate_keyframe_ids", "observation_age_s", "telemetry_receive_age_s",
                "clock_qualification", "physical_capture_time_uncertainty_s", "expires_at_utc",
                "valid_visual_pose", "heading_available", "navigation_usable")
        return {**{k: value[k] for k in keep if k in value}, "visual_comparison": comparison,
                "model_read_operation": "robot.map.radio.read", "motion_commands": False}

    def status(self, *, include_observations=False):
        result = {"enabled": self.enabled, "receiver_calibration_id": self.receiver_calibration_id,
                  "source": self.source.status(), "localization": self.prediction()}
        with self._condition:
            result.update(error=self._error, model=copy.deepcopy(self._model_status),
                          learning=copy.deepcopy(self._last_training), pose_history_count=len(self._poses))
        if include_observations:
            result["telemetry"] = self.source.snapshot()
        return result

    def read_map(self, map_id, calibration_id, keyframes, **kwargs):
        if self.model is None:
            return {"availability": "unavailable", "reason": self._idle_state(), "entries": []}
        with self._model_lock:
            return self.model.read_map(map_id, calibration_id, keyframes, **kwargs)

    def close(self):
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        if self._thread:
            self._thread.join()
        self.source.close()
        if self.model is not None:
            with self._model_lock:
                self.model.close()
