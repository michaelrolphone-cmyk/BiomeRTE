#!/usr/bin/env python3
"""Mac perception service. The independent driving brain consumes its observations."""
from __future__ import annotations

import argparse
import copy
import fcntl
import json
import logging
import os
import signal
import shutil
import socketserver
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from camera_source import CameraSource, utc_now
from detector import ObjectDetector
from ground_projection import calibration_errors, project_ground
from vision_config import DEFAULT_STATE, atomic_json, load_config
from vision_store import VisionStore
from metric_sessions import METRIC_CAPABILITIES, MetricCalibration
from radio_tracking import RadioTracking
from ahrs_tracking import AHRSTracking
from camera_telemetry import CameraHTTPError
from navigation_frames import read_robot_pose

CAPABILITIES = [
    "robot.vision.snapshot", "robot.vision.frame", "robot.vision.detections",
    "robot.vision.geometry", "robot.vision.metadata", "robot.vision.status",
    "robot.map.read", "robot.map.changes", "robot.map.checkpoint",
    "robot.map.create", "robot.map.open", "robot.vision.calibration",
    "robot.vision.project_ground",
    "robot.radio.status", "robot.radio.localize", "robot.map.radio.read",
    "robot.camera.light.status", "robot.camera.light.set",
    "robot.ahrs.status", "robot.ahrs.snapshot", "robot.ahrs.calibrate",
] + METRIC_CAPABILITIES
LOG = logging.getLogger("robot-vision")


def unknown_localization(reason):
    return {"status": reason, "valid": False, "T_map_camera": None,
            "pose_origin": "camera_optical", "units": "arbitrary",
            "scale_status": "unobserved", "robot_pose": None,
            "robot_pose_status": "camera_to_robot_extrinsics_unavailable",
            "covariance": None}


class VisionService:
    def __init__(self, config, state_dir, *, camera=None, detector=None, backend_factory=None,
                 radio_source=None, radio_model=None, ahrs_source=None):
        from leased_publish import LiveFrames
        self.live_frames = LiveFrames()
        from leased_maps import LiveMap
        self.live_maps = LiveMap()
        self.config = copy.deepcopy(config)
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.chmod(0o700)
        self.store = VisionStore(self.state_dir / "observations",
                                 max_observations=int(config.get("max_observations", 300)))
        self.camera = camera or CameraSource(config["camera"])
        self.radio = RadioTracking(config["camera"], config.get("radio", {}), self.state_dir,
                                   source=radio_source, model=radio_model)
        self.ahrs = AHRSTracking(config["camera"], config.get("ahrs", {}), source=ahrs_source)
        self.detector = detector or ObjectDetector(config.get("detector", {}), self.state_dir)
        if backend_factory is None:
            from slam_backend import SlamBackend
            backend_factory = SlamBackend
        self.backend_factory = backend_factory
        self.backend = None
        self._pipeline_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._started = time.monotonic()
        self._frames = 0
        self._skipped = 0
        self._error = None
        self._last_process_ms = None
        self._last_checkpoint_tick = time.monotonic()
        self._last_checkpoint_revision = None
        self._checkpoint_error = None
        self._checkpoint_cleanup_error = None
        self._checkpoint_future = None
        self._last_geometry = {}
        self._pending_changes = []
        self._reconcile_geometry = False
        self._recovery_error = None
        self._lifecycle_state = "starting"

        runtime_path = self.state_dir / "runtime.json"
        runtime = json.loads(runtime_path.read_text()) if runtime_path.exists() else {}
        saved = self.store.get_calibration(runtime.get("calibration_id"))
        if saved.get("availability") != "available":
            supplied = copy.deepcopy(config.get("calibration", {}))
            saved = self.store.save_calibration(supplied, supplied.get("calibration_id"))
        self.calibration = copy.deepcopy(saved["calibration"])
        self.calibration["calibration_id"] = saved["calibration_id"]
        map_id = runtime.get("map_id")
        if map_id:
            info = self.store.read_map(map_id, limit=1)["map"]
            associated = info.get("metadata", {}).get("calibration_id")
            if associated:
                record = self.store.get_calibration(associated)
                if record.get("availability") != "available":
                    raise ValueError("active_map_calibration_missing")
                self.calibration = copy.deepcopy(record["calibration"])
                self.calibration["calibration_id"] = record["calibration_id"]
        else:
            info = self.store.create_map(self._map_metadata())
            map_id = info["map_id"]
        self.map_id = map_id
        self.revision = info["revision"]
        self.session_id = None
        self._start_backend_and_session()
        self.metric = MetricCalibration(self.store)

    def _map_metadata(self, extra=None):
        return {
            **(extra or {}),
            "kind": "monocular_sparse_map", "units": "arbitrary",
            "scale_status": "unobserved",
            "calibration_id": self.calibration["calibration_id"],
            "camera_source_id": self.config["camera"].get("source_id"),
            "origin": "first_localized_camera",
            "occupancy": "unknown_without_supported_geometric_evidence",
        }

    def _prepare_backend(self, map_id, calibration, *, allow_load_failure=False):
        latest = self.store.latest_backend_checkpoint(map_id)
        artifact = latest.get("backend_artifact") if latest.get("availability") == "available" else None
        if artifact and artifact.get("availability") != "available":
            artifact = None
        stored_geometry = self.store.read_map(map_id, kinds=["keyframe", "sparse_point"], limit=1)["entities"]
        recovery_error = "stored_geometry_has_no_reusable_backend_checkpoint" if stored_geometry and not artifact else None
        cfg = {
            **self.config.get("slam", {}),
            "calibration": copy.deepcopy(calibration),
            "fps": self.config["camera"].get("expected_fps", 5.0),
            "pyslam_root": str(self.state_dir / "vendor/pyslam"),
        }
        backend = self.backend_factory(cfg, self.state_dir)
        try:
            if recovery_error is None:
                backend.start(map_id, checkpoint=artifact)
        except Exception as exc:
            backend.close()
            if not allow_load_failure:
                raise
            LOG.exception("Backend initialization failed; preserving map and observation service")
            backend = self.backend_factory(cfg, self.state_dir)
            recovery_error = f"backend_initialization_failed:{type(exc).__name__}"
        return backend, artifact, recovery_error

    def _activate_map(self, map_id, revision, calibration):
        # pySLAM has process-global feature settings and ID counters. Never keep
        # two native engines alive; on failure reload the saved previous map.
        previous = self.backend
        previous_identity = (self.map_id, self.revision, copy.deepcopy(self.calibration))
        self._lifecycle_state = "switching_map"
        if previous is not None:
            previous.close()
            # Observation production is excluded by the caller's pipeline lock.
            if self.session_id is not None:
                self.store.stop_session(self.session_id, reason="map_transition")
        self.backend = None
        backend = None
        session = None
        try:
            backend, artifact, recovery_error = self._prepare_backend(
                map_id, calibration, allow_load_failure=previous is None and self.session_id is None)
            session = self.store.start_session(
                map_id, camera=self.config["camera"], calibration=calibration["calibration_id"])
            atomic_json(self.state_dir / "runtime.json",
                        {"map_id": map_id, "calibration_id": calibration["calibration_id"]})
        except Exception:
            try:
                if session is not None:
                    self.store.stop_session(session["session_id"], reason="activation_failed")
            finally:
                if backend is not None:
                    backend.close()
            if previous is not None:
                prior_map, prior_revision, prior_calibration = previous_identity
                restored, saved, problem = self._prepare_backend(
                    prior_map, prior_calibration, allow_load_failure=True)
                self.backend = restored
                self.map_id, self.revision, self.calibration = previous_identity
                self.session_id = self.store.start_session(
                    prior_map, camera=self.config["camera"],
                    calibration=prior_calibration["calibration_id"])["session_id"]
                self._recovery_error = problem
                self._reconcile_geometry = saved is not None
                self.detector.reset_tracking()
                atomic_json(self.state_dir / "runtime.json",
                            {"map_id": prior_map, "calibration_id": prior_calibration["calibration_id"]})
                self._lifecycle_state = "running"
                self._reset_tracking_context()
            raise
        self.backend = backend
        self.map_id, self.revision = map_id, revision
        self.calibration = copy.deepcopy(calibration)
        self.session_id = session["session_id"]
        self._recovery_error = recovery_error
        self._reconcile_geometry = artifact is not None
        self._pending_changes = []
        self._last_geometry = {}
        self._last_checkpoint_revision = None
        self._lifecycle_state = "running"
        self._reset_tracking_context()

    def _radio_keyframes(self, map_id, revision=None):
        keyframes, cursor = {}, None
        while True:
            page = self.store.read_map(map_id, revision=revision, kinds=["keyframe"],
                                       cursor=cursor, limit=500)
            for entity in page["entities"]:
                keyframes[str(entity["entity_id"])] = copy.deepcopy(entity["data"])
            cursor = page.get("next_cursor")
            if not cursor:
                return keyframes

    def _reset_tracking_context(self):
        self.radio.set_context(self.map_id, self.calibration["calibration_id"],
                               self._radio_keyframes(self.map_id, self.revision), self.revision)
        self.ahrs.set_context(self.map_id, self.calibration["calibration_id"], self.session_id)

    def _start_backend_and_session(self):
        self._activate_map(self.map_id, self.revision, self.calibration)

    def _backend_status(self):
        if self.backend is None:
            return {"status": self._lifecycle_state, "valid": False}
        result = self.backend.status()
        if self._recovery_error:
            result = {**result, "status": "map_recovery_unavailable",
                      "valid": False, "reason": self._recovery_error}
        return result

    def start(self):
        self.metric.start_worker()
        self.radio.start()
        self.ahrs.start()
        self.camera.start()
        self._thread = threading.Thread(target=self._run, name="vision-processing", daemon=True)
        self._thread.start()
        return self

    def _run(self):
        try:
            self.detector.start()
        except Exception as exc:
            self._error = f"detector_start_failed:{type(exc).__name__}"
            LOG.exception("Detector startup failed")
        sequence = 0
        epoch = None
        while not self._stop.is_set():
            captured = self.camera.next_frame(sequence)
            if captured is None:
                continue
            self._skipped += max(0, captured.sequence-sequence-1)
            sequence = captured.sequence
            started = time.monotonic()
            try:
                with self._pipeline_lock:
                    if epoch is not None and epoch != captured.metadata["stream_epoch"]:
                        self.detector.reset_tracking()
                        if not self._recovery_error:
                            self.backend.invalidate_tracking("camera_stream_reconnected")
                    epoch = captured.metadata["stream_epoch"]
                    self.process_frame(captured)
                    self._frames += 1
                    self._last_process_ms = (time.monotonic()-started)*1000
                    self._error = None
                    self._maybe_checkpoint()
            except Exception as exc:
                self._error = f"processing_failed:{type(exc).__name__}"
                LOG.exception("Frame processing failed")

    def _reconcile_changes(self, changes, geometry):
        changes = list(changes or [])
        if geometry.get("complete_snapshot") is True:
            alive = {(x["kind"], str(x["entity_id"])) for x in changes
                     if x["op"] == "upsert" and x["kind"] in ("keyframe", "sparse_point")}
            cursor = None
            while True:
                page = self.store.read_map(self.map_id, revision=self.revision,
                                           kinds=["keyframe", "sparse_point"], cursor=cursor)
                for entity in page["entities"]:
                    identity = (entity["kind"], str(entity["entity_id"]))
                    if identity not in alive:
                        changes.insert(0, {"op": "delete", "kind": identity[0],
                                           "entity_id": identity[1], "data": {}})
                cursor = page.get("next_cursor")
                if not cursor:
                    break
            self._reconcile_geometry = False
        return changes

    def _commit_changes(self, changes, reason="mapping"):
        combined = {}
        for change in [*self._pending_changes, *changes]:
            combined[(change["kind"], str(change["entity_id"]))] = change
        self._pending_changes = list(combined.values())
        if self._pending_changes:
            previous = self.revision
            update = self.store.apply_map_update(
                self.map_id, self.revision, self._pending_changes, reason=reason,
                metadata={"session_id": self.session_id})
            self.revision = update["revision"] if isinstance(update, dict) else int(update)
            self.radio.update_geometry(self.map_id, self.calibration["calibration_id"],
                                       self._pending_changes, self.revision)
            self.live_maps.publish(self.map_id, previous, self.revision, self._pending_changes)
            self._pending_changes.clear()

    def process_frame(self, captured):
        """One coherent observation; callers hold the pipeline lock around backend state."""
        import cv2

        frame = {**captured.metadata, "calibration_id": self.calibration["calibration_id"],
                 "encoding_origin": "jpeg_reencoded_from_this_decoded_frame",
                 "receive_decode_monotonic_s": captured.decoded_monotonic}
        frame_id = frame["frame_id"]
        gray = cv2.cvtColor(captured.image, cv2.COLOR_BGR2GRAY)
        mean_luma = float(gray.mean())
        frame["lighting"] = {"mean_luma_0_255": mean_luma, "low_light_observed": mean_luma < 35.0,
                             "assessment": "image_brightness_not_measured_illumination",
                             "light_control_operation": "robot.camera.light.set"}
        ok, encoded = cv2.imencode(".jpg", captured.image, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            raise RuntimeError("frame_encoding_failed")
        detections = self.detector.detect(captured.image, frame_id, captured.decoded_monotonic)
        frame["detection_metadata"] = {key: value for key, value in detections.items() if key != "items"}
        size = [frame["width"], frame["height"]]
        errors = calibration_errors(self.calibration, size)
        if self._recovery_error:
            result = {"localization": unknown_localization("map_recovery_unavailable"),
                      "geometry": {"available": False, "reason": self._recovery_error},
                      "map_changes": []}
        elif errors:
            result = {"localization": unknown_localization("awaiting_calibration"),
                      "geometry": {"available": False, "reason": "calibration_unavailable", "details": errors},
                      "map_changes": []}
        else:
            try:
                if hasattr(self.backend, "set_relocalization_prior"):
                    self.backend.set_relocalization_prior(
                        self.radio.prior(now_s=captured.decoded_monotonic), map_id=self.map_id)
                result = self.backend.track(captured.image, frame_id, captured.decoded_monotonic)
            except Exception as exc:
                self.backend.invalidate_tracking("backend_track_error")
                result = {"localization": unknown_localization("error"),
                          "geometry": {"available": False, "reason": f"slam_failed:{type(exc).__name__}"},
                          "map_changes": []}
        geometry = result.get("geometry", {})
        changes = self._reconcile_changes(result.get("map_changes", []), geometry)
        anchor = result.get("observation_anchor")
        exact_anchor = bool(anchor and anchor.get("source_frame_id") == frame_id)
        semantic_change = None
        if exact_anchor:
            semantic_change = {
                "op": "upsert", "kind": "semantic_observation", "entity_id": frame_id,
                "data": {"frame_id": frame_id, "anchor": anchor,
                         "calibration_id": self.calibration["calibration_id"],
                         "detections": detections, "global_positions": None,
                         "identity_status": "observations_not_persistent_object_identities",
                         "geometry_rule": "resolve_anchor_at_requested_map_revision"},
            }
        self._commit_changes(changes, result.get("map_change_reason", "mapping"))
        localization = copy.deepcopy(result.get("localization") or unknown_localization("unavailable"))
        localization.update(map_id=self.map_id, map_revision=self.revision, frame_id=frame_id)
        localization.setdefault("covariance", None)
        localization.setdefault("robot_pose", None)
        localization.setdefault("robot_pose_status", "camera_to_robot_extrinsics_unavailable")
        if not localization.get("valid"):
            localization["T_map_camera"] = None
        self._last_geometry = copy.deepcopy(geometry)
        spatial = {
            "sparse_geometry": geometry,
            "ground_projection": {
                "availability": "available_on_confirmed_contact_query" if not calibration_errors(
                    self.calibration, size, require_mounting=True) else "unavailable",
                "height_m": self.calibration.get("mounting", {}).get("height_m"),
                "pitch_down_deg": self.calibration.get("mounting", {}).get("pitch_down_deg"),
                "calibration_id": self.calibration["calibration_id"],
            },
            "occupancy": {"availability": "unavailable", "default_state": "unknown",
                          "reason": "sparse_features_and_detections_do_not_certify_free_space"},
            "traversability": {"availability": "unavailable", "reason": "not_estimated"},
            "dropoffs": {"availability": "unavailable", "reason": "not_estimated"},
            "metric_depth": {"availability": "unavailable", "reason": "no_metric_depth_sensor_or_qualified_scale"},
            "robot_velocity": {"availability": "unavailable", "reason": "no_continuous_metric_odometry"},
            "radio": self.radio.evidence(localization, now_s=captured.decoded_monotonic),
            "ahrs": self.ahrs.evidence(frame, localization),
        }
        frame["processing_complete_time"] = utc_now()
        tracks = [{"track_id": d["track_id"], "detection_id": d["detection_id"],
                   "motion_class": d["motion_class"], "image_motion": d["image_motion"]}
                  for d in detections.get("items", []) if d.get("track_id")]
        snapshot = self.store.publish_observation(
            self.session_id, frame, detections.get("items", []), localization,
            tracks=tracks, spatial_evidence=spatial,
            keyframe_id=anchor["keyframe_id"] if exact_anchor else None,
            durable=exact_anchor, image_bytes=encoded.tobytes())
        if exact_anchor:
            self.store.pin_frame(frame_id, f"keyframe:{self.map_id}:{anchor['keyframe_id']}")
            semantic_change["data"]["snapshot_id"] = snapshot["snapshot_id"]
            self._commit_changes([semantic_change], "semantic_observation")
        self.radio.record_pose(frame, localization)
        self.ahrs.record_pose(frame, localization)
        try:
            self.metric.offer(snapshot)
        except Exception as exc:
            self.metric.record_error("capture_offer:" + type(exc).__name__ + ":" + str(exc))
            LOG.exception("Metric capture enqueue failed; coherent observation remains available")
        self.live_frames.publish({
            "frame_id": frame_id, "source_timestamp": frame.get("source_timestamp", frame.get("capture_time")),
            "source_timestamp_status": frame.get("capture_time_status", "unknown"),
            "receive_decode_timestamp": frame.get("receive_decode_time"),
            "processing_timestamp": frame["processing_complete_time"], "processed_monotonic": time.monotonic(),
            "calibration_revision": self.calibration["calibration_id"], "map_revision": self.revision,
            "map_id": self.map_id, "session_id": self.session_id,
            "detections": snapshot.get("detections", []), "pose": snapshot.get("localization", {}),
            "radio": self.radio.status(), "ahrs": self.ahrs.status(),
            "map_snapshot_required_on_revision_gap": True,
        }, encoded.tobytes())
        return snapshot

    def _freshness(self, snapshot):
        frame = snapshot.get("frame", {})
        now = datetime.now(timezone.utc)
        stamp = frame.get("receive_decode_time")
        current_session = snapshot.get("session_id") == self.session_id
        threshold = float(self.config.get("freshness_threshold_s", 2.0))
        age, valid_until = None, None
        try:
            parsed = datetime.fromisoformat(stamp) if stamp else None
            if parsed is not None and parsed.tzinfo is not None:
                age = max(0.0, (now-parsed).total_seconds())
                valid_until = (parsed+timedelta(seconds=threshold)).isoformat()
        except (ValueError, TypeError):
            pass
        tick = frame.get("receive_decode_monotonic_s")
        if current_session and isinstance(tick, (int, float)):
            age = max(0.0, time.monotonic()-tick)
        return {
            "receive_decode_age_s": age, "capture_age_s": None,
            "assessed_at_mac_utc": now.isoformat(),
            "valid_until_mac_utc": valid_until,
            "is_current_session": current_session,
            "is_fresh": current_session and self._lifecycle_state == "running"
                        and age is not None and age <= threshold,
            "lifecycle": self._lifecycle_state,
            "freshness_threshold_s": threshold,
            "delivery_latency_included": False,
            "reason": "Age is at query time; recheck receipt time after transport delivery.",
        }

    def _snapshot(self, inputs):
        snapshot = (self.store.get_snapshot(inputs["snapshot_id"]) if inputs.get("snapshot_id")
                    else self.store.latest_snapshot(session_id=self.session_id))
        if snapshot.get("availability") == "unavailable":
            return snapshot
        result = {**snapshot, "availability": "available", "freshness": self._freshness(snapshot),
                  "metric_localization": self.metric.metric_localization(snapshot)}
        result["robot_localization"] = read_robot_pose(
            result, self.state_dir / "navigation-frames.json", delivery_elapsed_s=0.0,
            anchor_resolver=self.metric.repo.resolve_floor_anchors)
        return result

    def _binding(self, snapshot):
        return {key: snapshot.get(key) for key in
                ("availability", "reason", "snapshot_id", "frame_id", "session_id",
                 "map_id", "map_revision", "published_at", "freshness")}

    def _maybe_checkpoint(self):
        interval = float(self.config.get("checkpoint_interval_s", 60))
        if time.monotonic() - self._last_checkpoint_tick < interval:
            return None
        try:
            if self.revision != self._last_checkpoint_revision:
                return (self._schedule_checkpoint(origin="automatic")
                        if hasattr(self.backend, "capture_checkpoint") else
                        self._checkpoint(origin="automatic"))
        finally:
            # A failed save must not be retried on each arriving camera frame.
            self._last_checkpoint_tick = time.monotonic()

    def _wait_checkpoint(self):
        future = self._checkpoint_future
        if future is not None:
            try:
                future.result()
            except Exception:
                LOG.exception("Background checkpoint failed; preserving prior checkpoint")

    def _schedule_checkpoint(self, *, origin="automatic"):
        from concurrent.futures import Future
        from slam_backend import BackendNotReady
        with self._pipeline_lock:
            if self._checkpoint_future is not None and not self._checkpoint_future.done():
                return self._checkpoint_future
            target = self.state_dir / "maps" / self.map_id / ("backend-" + uuid.uuid4().hex)
            prepared = None
            ticket = None
            try:
                try:
                    ticket = self.backend.capture_checkpoint(target)
                except BackendNotReady:
                    # Preserve the existing metadata-only checkpoint behavior.
                    return self._checkpoint(origin=origin)
                geometry = ticket["geometry"]
                changes = self._reconcile_changes(ticket["map_changes"], geometry)
                self._commit_changes(changes, "checkpoint_reconciliation")
                revision = self.revision
                prepared = self.store.prepare_checkpoint(self.map_id, metadata={
                    "session_id": self.session_id,
                    "calibration_id": self.calibration["calibration_id"],
                    "checkpoint_origin": origin,
                    "backend_status": self._backend_status(),
                    "map_revision": revision,
                    "geometry_matches_backend_state": True,
                    "backend_checkpoint": {"format": "pyslam-system-state",
                                           "capture": "immutable_map_and_fifo_database_barrier"},
                })
            except Exception as exc:
                self._checkpoint_error = "checkpoint_failed:" + type(exc).__name__
                if ticket is None:
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    # A queued native save must settle before another can begin,
                    # even when freezing the SQLite revision failed.
                    failed = Future()
                    self._checkpoint_future = failed
                    backend = self.backend
                    # Bind the exception: Python clears an except variable.
                    error = exc
                    def cleanup(error=error):
                        try:
                            backend.write_checkpoint(ticket)
                        except Exception:
                            LOG.exception("Native checkpoint cleanup failed")
                        finally:
                            shutil.rmtree(target, ignore_errors=True)
                            failed.set_exception(error)
                    threading.Thread(target=cleanup, name="checkpoint-cleanup", daemon=True).start()
                raise
            future = Future()
            self._checkpoint_future = future
            backend = self.backend
            def finish():
                started = time.monotonic()
                try:
                    artifact = backend.write_checkpoint(ticket)
                    saved = self.store.finish_checkpoint(prepared, artifact["path"])
                    self._last_checkpoint_revision = revision
                    self._checkpoint_error = None
                    self._checkpoint_cleanup_error = None
                    try:
                        saved["retention"] = self.store.retire_superseded_checkpoints()
                        if saved["retention"]["cleanup_errors"]:
                            self._checkpoint_cleanup_error = "checkpoint_retirement_cleanup_failed"
                    except Exception:
                        self._checkpoint_cleanup_error = "checkpoint_retirement_failed"
                        LOG.exception("Asynchronous checkpoint retention failed")
                    LOG.info("Asynchronous checkpoint saved revision=%s write_seconds=%.3f",
                             revision, time.monotonic()-started)
                    future.set_result(saved)
                except Exception as exc:
                    self._checkpoint_error = "checkpoint_failed:" + type(exc).__name__
                    self.store.discard_checkpoint(prepared)
                    LOG.exception("Asynchronous checkpoint failed; prior checkpoint retained")
                    future.set_exception(exc)
                finally:
                    shutil.rmtree(target, ignore_errors=True)
            threading.Thread(target=finish, name="checkpoint-writer", daemon=True).start()
            return future

    def _checkpoint(self, *, origin="automatic"):
        self._wait_checkpoint()
        target = self.state_dir / "maps" / self.map_id / ("backend-"+uuid.uuid4().hex)
        from slam_backend import BackendNotReady
        self._checkpoint_cleanup_error = None
        try:
            try:
                artifact = self.backend.checkpoint(target)
            except BackendNotReady as exc:
                artifact = {"availability": "unavailable", "reason": str(exc), "path": None}
            if not isinstance(artifact, dict):
                raise TypeError("backend_checkpoint_must_return_metadata")
            if artifact.get("map_changes") is not None:
                geometry = artifact.get("geometry", {})
                changes = self._reconcile_changes(artifact["map_changes"], geometry)
                self._commit_changes(changes, "checkpoint_reconciliation")
            saved = self.store.checkpoint(
                self.map_id, backend_artifact=artifact.get("path") if artifact.get("availability") == "available" else None,
                metadata={"session_id": self.session_id, "calibration_id": self.calibration["calibration_id"],
                          "checkpoint_origin": origin,
                          "backend_status": self._backend_status(),
                          "map_revision": self.revision,
                          "geometry_matches_backend_state": artifact.get("exact_saved_geometry", False),
                          "backend_checkpoint": {k: v for k, v in artifact.items()
                                                 if k not in ("map_changes", "geometry", "files", "path")}})
            self._last_checkpoint_revision = self.revision
            self._checkpoint_error = None
            try:
                saved["retention"] = self.store.retire_superseded_checkpoints()
                if saved["retention"]["cleanup_errors"]:
                    self._checkpoint_cleanup_error = "checkpoint_retirement_cleanup_failed"
            except Exception as exc:
                self._checkpoint_cleanup_error = "checkpoint_retirement_failed:" + type(exc).__name__
                LOG.exception("Checkpoint published but retirement failed")
            return saved
        except Exception as exc:
            self._checkpoint_error = "checkpoint_failed:" + type(exc).__name__
            raise
        finally:
            self._last_checkpoint_tick = time.monotonic()
            try:
                if target.exists():
                    # Only the service-owned staging directory is ephemeral;
                    # VisionStore keeps caller-owned input artifacts untouched.
                    shutil.rmtree(target)
            except OSError as exc:
                self._checkpoint_cleanup_error = "checkpoint_staging_cleanup_failed:" + type(exc).__name__
                LOG.exception("Checkpoint staging cleanup failed")


    def _robot_model(self, *, summary=False):
        try:
            definition = json.loads((self.state_dir / "robot-model.json").read_text())
            if (not isinstance(definition, dict) or definition.get("schema_version") != 1
                    or not isinstance(definition.get("model_id"), str)
                    or not definition["model_id"]
                    or isinstance(definition.get("revision"), bool)
                    or not isinstance(definition.get("revision"), int)
                    or definition["revision"] < 1):
                raise ValueError("invalid_robot_model_identity")
            # Configuration is reported evidence, not an estimator or odometry source.
            json.dumps(definition, allow_nan=False)
        except FileNotFoundError:
            return {"availability": "unavailable", "reason": "robot_model_not_configured"}
        except (OSError, ValueError, TypeError):
            return {"availability": "unavailable", "reason": "robot_model_configuration_invalid"}
        result = {"availability": "available", "model_id": definition["model_id"],
                  "revision": definition["revision"],
                  "qualification_status": definition.get("qualification_status", "unqualified"),
                  "source": "versioned_robot_configuration"}
        if not summary:
            result["definition"] = definition
        return result


    def query(self, operation, inputs=None):
        inputs = inputs or {}
        if operation not in CAPABILITIES:
            raise KeyError("unsupported_operation")
        if operation == "robot.ahrs.status":
            return self.ahrs.status()
        if operation == "robot.ahrs.snapshot":
            snapshot = self.ahrs.snapshot()
            if snapshot is None:
                return {"availability": "unavailable", "reason": "ahrs_snapshot_unavailable",
                        "status": self.ahrs.status()}
            return {"availability": "available", "snapshot": snapshot,
                    "interpretation": "inspect_each_sensor_validity_age_frame_and_units"}
        if operation == "robot.ahrs.calibrate":
            if inputs:
                raise ValueError("ahrs_calibrate_takes_no_inputs")
            try:
                receipt = self.ahrs.calibrate_gyro()
                return {"availability": "available", "calibration_command": receipt,
                        "motion_commands": False}
            except CameraHTTPError as exc:
                return {"availability": "unavailable", **exc.as_dict(),
                        "motion_commands": False}
        if operation == "robot.radio.status":
            return self.radio.status(include_observations=inputs.get("include_observations") is True)
        if operation == "robot.radio.localize":
            return self.radio.prediction()
        if operation == "robot.map.radio.read":
            map_id = inputs.get("map_id", self.map_id)
            revision = inputs.get("revision")
            info = self.store.read_map(map_id, revision=revision, kinds=["keyframe"], limit=1)["map"]
            calibration_id = info.get("metadata", {}).get("calibration_id")
            result = self.radio.read_map(
                map_id, calibration_id, self._radio_keyframes(map_id, info["revision"]),
                cursor=inputs.get("cursor"), limit=inputs.get("limit", 100),
                source_id=inputs.get("source_id"))
            return {**result, "map_id": map_id, "map_revision": info["revision"],
                    "calibration_id": calibration_id, "units": "arbitrary",
                    "clock_qualification": "approximate_host_receive_alignment"}
        if operation in ("robot.camera.light.status", "robot.camera.light.set"):
            try:
                state = (self.radio.source.set_light(inputs) if operation.endswith(".set")
                         else self.radio.source.light_status())
                return {"availability": "available", "camera_light": state,
                        "physical_verification": "use_reported_hardware_verified_and_optical_readback",
                        "motion_commands": False}
            except CameraHTTPError as exc:
                return {"availability": "unavailable", **exc.as_dict(),
                        "motion_commands": False}
        if operation in METRIC_CAPABILITIES:
            return self.metric.query(operation, inputs, map_id=self.map_id,
                                     calibration_id=self.calibration["calibration_id"])
        if operation == "robot.vision.status":
            return {
                "availability": "available", "state": self._lifecycle_state if not self._stop.is_set() else "stopping",
                "capabilities": CAPABILITIES,
                "streaming": {"available": not self._stop.is_set(), "lanes": ["vision", "map"],
                              "transport": "native_leased_stream", "per_message_actions": False},
                "map_id": self.map_id,
                "map_revision": self.revision, "session_id": self.session_id,
                "uptime_s": time.monotonic()-self._started, "processed_frames": self._frames,
                "skipped_camera_frames": self._skipped, "last_process_ms": self._last_process_ms,
                "processing_fps_since_start": self._frames/max(time.monotonic()-self._started, 1e-6),
                "error": self._error, "camera": self.camera.status(), "detector": self.detector.status(),
                "localization": self._backend_status(), "persistence": self.store.status(),
                "checkpoint_error": self._checkpoint_error,
                "checkpoint_running": self._checkpoint_future is not None and not self._checkpoint_future.done(),
                "checkpoint_cleanup_error": self._checkpoint_cleanup_error,
                "robot_model": self._robot_model(summary=True),
                "latest_checkpoint": self.store.latest_checkpoint(self.map_id),
                "latest_backend_checkpoint": self.store.latest_backend_checkpoint(self.map_id),
                "calibration": {"calibration_id": self.calibration["calibration_id"],
                                "errors": calibration_errors(self.calibration)},
                "metric_scale": self.metric.attachment_status(self.map_id),
                "metric_calibration": self.metric.status(map_id=self.map_id),
                "radio": self.radio.status(),
                "ahrs": self.ahrs.status(),
                "control": {"motion_commands": False, "brain": "external",
                            "camera_light": "explicit_authenticated_camera_api",
                            "gyro_calibration": "explicit_authenticated_camera_api"},
            }
        if operation == "robot.vision.metadata":
            return {
                "schema_version": 1, "capabilities": CAPABILITIES,
                "camera": self.config["camera"], "calibration": self.calibration,
                "robot_model": self._robot_model(),
                "detector": self.detector.status(),
                "coordinate_frames": {
                    "image_pixels": "u right, v down, full decoded image resolution",
                    "camera_optical": "x right, y down, z forward; pose at optical center",
                    "camera_ground": "x forward, y left, z up; origin directly below optical center",
                    "map": "backend visual frame in arbitrary units",
                    "map_metric": "same native map origin and axes, translated coordinates in meters; separately qualified scale, floor alignment unmeasured",
                    "robot": "qualified camera-derived pose in robot_localization; unavailable without matching metric floor and mounting attachments",
                    "imu": "native MPU axes; camera and robot mounting transforms unqualified",
                    "ahrs_reference": "gravity-aligned initial-yaw reference; relative yaw can drift",
                    "magnetometer": "native AK8963 axes; distinct from IMU axes; not calibrated compass"},
                "time": {"capture": "unknown", "source_media": "relative clock",
                         "receive_decode": "Mac UTC after decode", "processing": "Mac monotonic durations",
                         "ahrs": "causal HTTP receipt history with per-sensor age and network timing uncertainty; camera exposure alignment unqualified"},
                "map_layers": ["keyframe", "sparse_point", "semantic_observation", "radio_fingerprint"],
                "map_layer_readers": {"radio_fingerprint": "robot.map.radio.read"},
                "radio_model": {"learning": "time_aligned_independently_verified_visual_poses",
                                "inference": "adaptive_source_distributions_and_competing_spatial_modes",
                                "visual_relocalization": "candidate_prior_with_unchanged_geometry_and_visual_fallback",
                                "clock_qualification": "approximate_host_receive_alignment",
                                "navigation_usable": False, "heading_available": False,
                                "storage": "bounded_radio_sqlite_outside_vision_checkpoint_packages"},
                "ahrs_evidence": {
                    "use": "sensor-frame motion and tilt evidence attached to coherent observations",
                    "orientation": "Hamilton quaternion wxyz maps IMU axes into initial-yaw reference",
                    "reset_identity": ["receiver_id", "boot_id", "generation", "yaw_origin_uptime_ms"],
                    "acceleration": "m/s^2 including gravity; no position or velocity integration",
                    "altitude": "standard-pressure reference; not height above floor or map z",
                    "camera_pose_fusion": "unavailable_until_mounting_and_exposure_timing_are_qualified",
                    "navigation_usable": False,
                    "storage": "bounded_memory_history_and_existing_retained_observations",
                    "raw_disk_journal": False},
                "unknown_space_rule": "No detection or sparse point is not proof of free space.",
                "identity_rule": "Tracker IDs are local to one tracker epoch, not durable object identities.",
                "restart_rule": "New session; reuse map checkpoint; current pose requires fresh relocalization.",
                "metric_scale_rule": "Exact checkerboard keyframes or independently measured displacements; immutable applied reference, resolve and revalidate against requested map revision.",
                "metric_calibration_control": "Passive observations and guidance; vehicle motion belongs to the independent brain.",
                "retention": {"rolling_observations": self.config.get("max_observations", 300),
                              "map_keyframe_evidence": "durable_and_pinned",
                              "controller_frames": "uploaded_only_when_requested"},
            }
        if operation in ("robot.vision.snapshot", "robot.vision.frame",
                         "robot.vision.detections", "robot.vision.geometry"):
            if operation == "robot.vision.frame" and inputs.get("frame_id"):
                return {"frame": self.store.get_frame(inputs["frame_id"], include_data=True)}
            snapshot = self._snapshot(inputs)
            if snapshot.get("availability") == "unavailable":
                return snapshot
            if operation == "robot.vision.snapshot":
                if inputs.get("include_frame") is True:
                    result = self.store.get_snapshot_with_frame(snapshot_id=snapshot["snapshot_id"])
                    result["snapshot"] = snapshot
                    return result
                return {"availability": "available", "snapshot": snapshot}
            if operation == "robot.vision.frame":
                result = self.store.get_snapshot_with_frame(snapshot_id=snapshot["snapshot_id"])
                result.pop("snapshot", None)
                return {**self._binding(snapshot), **result}
            if operation == "robot.vision.detections":
                return {**self._binding(snapshot), "detections": snapshot["detections"],
                        "tracks": snapshot["tracks"],
                        "detection_metadata": snapshot["frame"].get("detection_metadata", {})}
            return {**self._binding(snapshot), "localization": snapshot["localization"],
                    "metric_localization": snapshot["metric_localization"],
                    "robot_localization": snapshot["robot_localization"],
                    "spatial_evidence": snapshot["spatial_evidence"]}
        if operation == "robot.map.read":
            return self.store.read_map(inputs.get("map_id", self.map_id), revision=inputs.get("revision"),
                                       bounds=inputs.get("bounds"), kinds=inputs.get("kinds"),
                                       cursor=inputs.get("cursor"), limit=inputs.get("limit", 500))
        if operation == "robot.map.changes":
            return self.store.read_deltas(inputs.get("map_id", self.map_id), inputs["since_revision"],
                                          to_revision=inputs.get("to_revision"), cursor=inputs.get("cursor"),
                                          limit=inputs.get("limit", 500))
        if operation == "robot.vision.project_ground":
            snapshot = self._snapshot(inputs)
            if snapshot.get("availability") == "unavailable":
                return snapshot
            frame = snapshot["frame"]
            record = self.store.get_calibration(frame["calibration_id"])
            calibration = copy.deepcopy(record.get("calibration") or {})
            calibration["calibration_id"] = record.get("calibration_id")
            result = project_ground(inputs.get("pixel"), calibration,
                                    [frame["width"], frame["height"]],
                                    confirmed_floor_contact=inputs.get("confirmed_floor_contact", False))
            return {**self._binding(snapshot), "projection": result}
        if operation == "robot.map.checkpoint" and hasattr(self.backend, "capture_checkpoint"):
            if inputs.get("map_id", self.map_id) != self.map_id:
                raise ValueError("only_active_map_can_be_checkpointed")
            value = self._schedule_checkpoint(origin="explicit")
            return value.result() if hasattr(value, "result") else value
        with self._pipeline_lock:
            if operation == "robot.map.checkpoint":
                if inputs.get("map_id", self.map_id) != self.map_id:
                    raise ValueError("only_active_map_can_be_checkpointed")
                return self._checkpoint(origin="explicit")
            if operation == "robot.map.create":
                return self.store.create_map(self._map_metadata(inputs.get("metadata")))
            if operation == "robot.map.open":
                return self._open_map(inputs["map_id"])
            if operation == "robot.vision.calibration":
                action = inputs.get("action", "get")
                if action == "get":
                    return self.store.get_calibration(inputs.get("calibration_id", self.calibration["calibration_id"]))
                if action != "set" or not isinstance(inputs.get("calibration"), dict):
                    raise ValueError("calibration_set_requires_calibration_object")
                calibration = copy.deepcopy(inputs["calibration"])
                errors = calibration_errors(calibration)
                if calibration.get("validated") is True and errors:
                    raise ValueError("invalid_calibration:" + ",".join(errors))
                record = self.store.save_calibration(calibration, calibration.get("calibration_id"))
                old_map = self.map_id
                self._checkpoint(origin="recovery")
                candidate = copy.deepcopy(record["calibration"])
                candidate["calibration_id"] = record["calibration_id"]
                metadata = {**self._map_metadata(), "calibration_id": candidate["calibration_id"],
                            "previous_map_id": old_map, "reason": "calibration_changed"}
                info = self.store.create_map(metadata)
                self._activate_map(info["map_id"], info["revision"], candidate)
                self.detector.reset_tracking()
                return {**record, "previous_map_id": old_map, "active_map_id": self.map_id,
                        "previous_map_preserved": True, "backend": self._backend_status()}
        raise KeyError("unsupported_operation")

    def _open_map(self, map_id):
        if map_id == self.map_id and not self._recovery_error:
            return {"map_id": map_id, "session_id": self.session_id,
                    "map_revision": self.revision, "backend": self._backend_status()}
        info = self.store.read_map(map_id, limit=1)["map"]
        calibration_id = info.get("metadata", {}).get("calibration_id")
        calibration = self.store.get_calibration(calibration_id)
        if calibration.get("availability") != "available":
            raise ValueError("map_calibration_missing")
        geometry = self.store.read_map(map_id, kinds=["keyframe", "sparse_point"], limit=1)["entities"]
        checkpoint = self.store.latest_backend_checkpoint(map_id)
        if geometry and checkpoint.get("backend_artifact", {}).get("availability") != "available":
            raise ValueError("map_backend_checkpoint_unavailable")
        self._checkpoint(origin="recovery")
        candidate = copy.deepcopy(calibration["calibration"])
        candidate["calibration_id"] = calibration_id
        self._activate_map(map_id, info["revision"], candidate)
        self.detector.reset_tracking()
        return {"map_id": self.map_id, "session_id": self.session_id,
                "map_revision": self.revision, "backend": self._backend_status()}

    def close(self):
        self.live_frames.close()
        self.live_maps.close()
        self._stop.set()
        self.camera.close()
        if self._thread:
            self._thread.join()
        try:
            self.radio.close()
        except Exception:
            LOG.exception("Radio shutdown failed; continuing vision shutdown")
        try:
            self.ahrs.close()
        except Exception:
            LOG.exception("AHRS shutdown failed; continuing vision shutdown")
        self.metric.close()
        self._wait_checkpoint()
        with self._pipeline_lock:
            try:
                # The final SQLite backup must include the completed session.
                if self.session_id is not None:
                    self.store.stop_session(self.session_id, reason="service_shutdown")
                self._checkpoint(origin="recovery")
            finally:
                try:
                    if self.backend is not None:
                        self.backend.close()
                finally:
                    self.store.close()


class LocalServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    # main() closes the server before checkpointing/closing the service. Drain
    # accepted mutations so their persistence precedes that final checkpoint.
    daemon_threads = False
    block_on_close = True


def handler_for(service):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/v1/live", "/v1/live/map"):
                self._respond(404, {"error": {"code": "not_found"}})
                return
            self.connection.settimeout(1)
            try:
                feed=service.live_maps if self.path.endswith('/map') else service.live_frames
                with feed.subscribe() as inbox:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    while not service._stop.is_set() and not feed.closed.is_set():
                        try:
                            value=inbox.get(timeout=1)
                        except __import__('queue').Empty:
                            continue
                        self.wfile.write(value)
                        self.wfile.flush()
            except (OSError, RuntimeError):
                self.close_connection=True

        def do_POST(self):
            if self.path != "/v1/query":
                self._respond(404, {"error": {"code": "not_found", "message": "Unknown route"}})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 1024*1024:
                    raise ValueError("invalid_request_size")
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict) or not isinstance(body.get("inputs", {}), dict):
                    raise ValueError("invalid_request")
                result = service.query(body["operation"], body.get("inputs", {}))
                self._respond(200, result)
            except KeyError as exc:
                self._respond(404, {"error": {"code": "not_found", "message": str(exc)}})
            except (ValueError, TypeError) as exc:
                self._respond(400, {"error": {"code": "invalid_request", "message": str(exc)}})
            except Exception:
                LOG.exception("Query failed")
                self._respond(500, {"error": {"code": "query_failed", "message": "See local service log"}})

        def _respond(self, status, value):
            data = json.dumps(value, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass
    return Handler


def watch_parent(parent_pid, stop):
    """Request ordinary shutdown if this app-owned child is reparented."""
    while not stop.is_set():
        if os.getppid() != parent_pid:
            LOG.warning("Native app parent exited; stopping its vision child")
            stop.set()
            return
        stop.wait(1.0)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--parent-pid", type=int,
                        help="Native app parent; request graceful shutdown if reparented.")
    args = parser.parse_args(argv)
    if args.parent_pid is not None and args.parent_pid <= 1:
        parser.error("--parent-pid must identify a real parent process")
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop = threading.Event()
    watcher = None
    previous_handlers = {}
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[sig] = signal.signal(sig, lambda *_args: stop.set())
        if args.parent_pid is not None:
            if os.getppid() != args.parent_pid:
                LOG.warning("Native app parent is already absent; not starting a vision child")
                return
            watcher = threading.Thread(target=watch_parent, args=(args.parent_pid, stop),
                                       name="vision-parent-lifetime", daemon=True)
            watcher.start()
        args.state.mkdir(parents=True, exist_ok=True)
        with (args.state/"service.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            service = None
            server = None
            server_started = False
            socket_path = args.state/"vision.sock"
            try:
                if stop.is_set():
                    return
                service = VisionService(load_config(args.state, args.config), args.state)
                if stop.is_set():
                    return
                socket_path.unlink(missing_ok=True)
                server = LocalServer(str(socket_path), handler_for(service))
                socket_path.chmod(0o600)
                server_thread = threading.Thread(target=server.serve_forever,
                                                 name="vision-api", daemon=True)
                server_thread.start()
                server_started = True
                service.start()
                stop.wait()
            finally:
                try:
                    if server is not None:
                        if service is not None:
                            service.live_frames.close()
                            service.live_maps.close()
                        if server_started:
                            server.shutdown()
                        server.server_close()
                finally:
                    try:
                        if service is not None:
                            service.close()
                    finally:
                        if server is not None:
                            socket_path.unlink(missing_ok=True)
    finally:
        stop.set()
        if watcher is not None:
            watcher.join()
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    main()
