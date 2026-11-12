"""Passive metric-scale calibration and metric views for the independent brain.

A fixed checkerboard gives metric camera poses; exact native keyframes bind those
measurements to a persistent SLAM map. No function in this module drives a robot.
The worker's durable keyframe queue is separate from latest-frame visibility:
live preview can coalesce frames, but selected calibration evidence cannot vanish
when a newer frame arrives. No continuous video archive is introduced.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import threading
from datetime import datetime, timezone
import uuid

from ground_projection import calibration_errors
from metric_store import MetricStore
from metric_scale import (
    board_pose_candidates, estimate_metric_attachment, estimate_displacement_scale, validate_policy,
)

METRIC_CAPABILITIES = [
    "robot.map.scale.start", "robot.map.scale.status", "robot.map.scale.capture",
    "robot.map.scale.fit", "robot.map.scale.apply", "robot.map.scale.cancel",
    "robot.map.scale.reference", "robot.map.metric.read",
]


def _now():
    return datetime.now(timezone.utc).isoformat()


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _positive(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(label + "_must_be_positive_finite")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(label + "_must_be_positive_finite")
    return float(value)


def _reference(value, reference_id=None):
    if not isinstance(value, dict):
        raise ValueError("checkerboard_reference_required")
    ref = copy.deepcopy(value)
    corners = ref.get("inner_corners", [9, 6])
    if (not isinstance(corners, list) or len(corners) != 2 or
            any(isinstance(x, bool) or not isinstance(x, int) or x < 3 for x in corners)):
        raise ValueError("inner_corners_requires_two_integers_at_least_three")
    if corners[0] == corners[1]:
        raise ValueError("square_checkerboard_patterns_not_supported_use_rectangular_pattern")
    ref["inner_corners"] = corners
    ref["square_size_m"] = _positive(ref.get("square_size_m"), "square_size_m")
    if type(ref.get("size_verified", False)) is not bool:
        raise ValueError("size_verified_must_be_boolean")
    ref["size_verified"] = ref.get("size_verified", False)
    if not isinstance(ref.get("measurement_source"), str) or not ref["measurement_source"].strip():
        raise ValueError("physical_size_measurement_source_required")
    if ref.get("stationary") is not True:
        raise ValueError("reference_requires_fixed_stationary_board")
    supplied = ref.get("reference_board_id")
    if reference_id is not None and supplied not in (None, reference_id):
        raise ValueError("reference_board_identity_is_immutable")
    ref["reference_board_id"] = reference_id or supplied or str(uuid.uuid4())
    if not isinstance(ref["reference_board_id"], str) or not ref["reference_board_id"]:
        raise ValueError("reference_board_id_must_be_nonempty")
    return ref


class MetricCalibration:
    def __init__(self, store):
        self.store = store
        self.repo = MetricStore(store)
        self._lock = threading.RLock()
        self._event = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._preview = {}
        self._visibility = {}
        self._latest_keyframe = {}
        self._latest_frame = {}
        self._error = None
        # Solutions are derived from immutable attachments and exact geometry.
        # Cache only the most recent solution for each attachment, never a
        # current pose. Historical reads can always recompute their solution.
        self._solutions = {}
        self.recover_sessions()

    def record_error(self, reason):
        with self._lock:
            self._error = str(reason)

    def recover_sessions(self):
        """Resume a committed sample whose fit/application was interrupted."""
        with self._lock:
            for session in self.repo.sessions():
                if (session["state"] not in ("collecting", "ready", "provisional") or
                        not self.repo.samples(session["session_id"])):
                    continue
                try:
                    self._fit_locked(session)
                except Exception as exc:
                    # Calibration recovery must not prevent camera perception.
                    self._error = "metric_recovery:" + type(exc).__name__ + ":" + str(exc)

    def start_worker(self):
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="metric-calibration", daemon=True)
                self._thread.start()
                self._event.set()

    def close(self):
        self._stop.set()
        self._event.set()
        if self._thread is not None:
            self._thread.join()

    def _session(self, session_id):
        session = self.repo.get_session(session_id)
        if session is None:
            raise KeyError("metric_session_not_found")
        return session

    @staticmethod
    def _editable(session):
        if session["state"] in ("applied", "canceled"):
            raise ValueError("metric_session_is_" + session["state"])

    def start_session(self, map_id, calibration_id, inputs):
        method = inputs.get("method")
        if method not in ("checkerboard", "measured_distance"):
            raise ValueError("method_requires_checkerboard_or_measured_distance")
        record = self.store.get_calibration(calibration_id)
        if record.get("availability") != "available" or calibration_errors(record["calibration"]):
            raise ValueError("validated_camera_intrinsics_required")
        info = self.store.read_map(map_id, kinds=[], limit=1)["map"]
        if info["metadata"].get("calibration_id") != calibration_id:
            raise ValueError("metric_map_calibration_mismatch")
        if not isinstance(inputs.get("policy", {}), dict):
            raise ValueError("policy_must_be_object")
        for name in ("auto_apply", "auto_capture"):
            if name in inputs and type(inputs[name]) is not bool:
                raise ValueError(name + "_must_be_boolean")
        with self._lock:
            active = [s for s in self.repo.sessions() if s["map_id"] == map_id
                      and s["state"] in ("collecting", "ready", "provisional")]
            if active:
                raise ValueError("existing_metric_session_requires_cancel_or_apply:" +
                                 active[-1]["session_id"])
            reference = _reference(inputs.get("reference")) if method == "checkerboard" else None
            session = {
                "session_id": str(uuid.uuid4()), "map_id": map_id,
                "calibration_id": calibration_id, "method": method,
                "state": "collecting", "created_at": _now(), "updated_at": _now(),
                "reference": reference, "reference_revision": 1,
                "policy": validate_policy(inputs.get("policy", {})),
                "auto_capture": inputs.get("auto_capture", method == "checkerboard"),
                "auto_apply": inputs.get("auto_apply", False),
                "fit_inputs": {}, "candidate": None,
                "validation_assignment": "every_third_accepted_automatic_keyframe_is_held_out",
                "motion_control": "external_brain_only",
            }
            self.repo.save_session(session)
            self._event.set()
            return self.status(session["session_id"])

    def offer(self, snapshot):
        """Called after coherent frame/map publication; never runs image detection."""
        map_id = snapshot["map_id"]
        with self._lock:
            self._latest_frame[map_id] = snapshot["frame_id"]
            if snapshot.get("keyframe_id") is not None:
                self._latest_keyframe[map_id] = {
                    key: snapshot.get(key) for key in (
                        "snapshot_id", "frame_id", "keyframe_id", "map_id", "map_revision",
                        "calibration_id", "published_at")}
            for session in self.repo.sessions():
                if (session["map_id"] != map_id or session["state"] != "collecting" or
                        session["calibration_id"] != snapshot.get("calibration_id") or
                        session["method"] != "checkerboard"):
                    continue
                self._preview[session["session_id"]] = snapshot["snapshot_id"]
                if session["auto_capture"] and snapshot.get("keyframe_id") is not None:
                    self._check_snapshot(session, snapshot)
                    self.repo.enqueue(session, snapshot)
                self._event.set()

    def _check_snapshot(self, session, snapshot):
        if snapshot.get("availability") == "unavailable":
            raise ValueError("capture_snapshot_unavailable")
        if (snapshot.get("map_id") != session["map_id"] or
                snapshot.get("calibration_id") != session["calibration_id"]):
            raise ValueError("capture_map_or_calibration_mismatch")
        if snapshot.get("keyframe_id") is None or not snapshot.get("localization", {}).get("valid"):
            raise ValueError("capture_requires_exact_localized_keyframe_snapshot")
        stub = {"observation_id": snapshot["snapshot_id"], "frame_id": snapshot["frame_id"],
                "keyframe_id": str(snapshot["keyframe_id"])}
        _, _, missing = self.repo.resolve_keyframes(session["map_id"], [stub], snapshot["map_revision"])
        if missing:
            raise ValueError("capture_keyframe_source_frame_mismatch")

    def capture(self, session_id, snapshot_id, role=None, label=None):
        with self._lock:
            session = self._session(session_id)
            self._editable(session)
            if role not in (None, "training", "validation"):
                raise ValueError("role_requires_training_or_validation")
            if session["method"] == "measured_distance":
                if label not in ("start", "end", "validation_start", "validation_end"):
                    raise ValueError("measured_capture_requires_endpoint_label")
                expected = "validation" if label.startswith("validation_") else "training"
                if role not in (None, expected):
                    raise ValueError("endpoint_label_role_mismatch")
                role = expected
            elif label is not None:
                raise ValueError("checkerboard_capture_does_not_use_endpoint_labels")
            snapshot = self.store.get_snapshot(snapshot_id)
            self._check_snapshot(session, snapshot)
            job = self.repo.enqueue(session, snapshot, role=role, label=label)
            if job["state"] == "queued":
                session.update(state="collecting", candidate=None, updated_at=_now())
                self.repo.save_session(session)
                self._event.set()
            return {"availability": "available", "capture": job,
                    "session_id": session_id, "motion_commands": False}

    def _detect(self, session, snapshot):
        import cv2
        import numpy as np

        record = self.store.get_calibration(session["calibration_id"])
        calibration = record["calibration"]
        frame = self.store.get_frame(snapshot["frame_id"], include_data=True)
        if frame.get("availability") != "available":
            raise ValueError("calibration_evidence_image_unavailable")
        image = cv2.imdecode(np.frombuffer(base64.b64decode(frame["data_base64"]),
                                          dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if image is None or list(image.shape[::-1]) != [calibration["width"], calibration["height"]]:
            raise ValueError("calibration_evidence_dimensions_mismatch")
        ref = session["reference"]
        found, corners = cv2.findChessboardCornersSB(
            image, tuple(ref["inner_corners"]), flags=cv2.CALIB_CB_NORMALIZE_IMAGE)
        visibility = {
            "visible": bool(found), "frame_id": snapshot["frame_id"],
            "snapshot_id": snapshot["snapshot_id"], "observed_at": _now(),
            "receive_decode_time": snapshot["frame"].get("receive_decode_time"),
            "exact_keyframe": snapshot.get("keyframe_id") is not None,
            "localization_valid": bool(snapshot.get("localization", {}).get("valid")),
            "reference_size_verified": ref["size_verified"],
        }
        if not found:
            return visibility, None, []
        points = corners.reshape(-1, 2).astype(float)
        candidates = board_pose_candidates(points.tolist(), calibration, ref)
        visibility.update(
            corners_px=points.tolist(), center_px=points.mean(axis=0).tolist(),
            bounds_px=[*points.min(axis=0).tolist(), *points.max(axis=0).tolist()],
            image_size_px=[calibration["width"], calibration["height"]],
            pose_candidate_count=len(candidates),
        )
        if candidates:
            center = np.array([(ref["inner_corners"][0]-1)*ref["square_size_m"]/2,
                               (ref["inner_corners"][1]-1)*ref["square_size_m"]/2, 0])
            distances = [float(np.linalg.norm(np.array(c["T_board_camera"])[:3, 3]-center))
                         for c in candidates]
            visibility["board_center_range"] = {
                "estimate": min(distances), "units": "m",
                "qualification": "measured_reference" if ref["size_verified"] else "nominal_size_only",
                "pose_ambiguity_unresolved": len(candidates) > 1,
            }
        return visibility, points.tolist(), candidates

    def process_pending(self):
        """One durable job, or one coalesced visibility preview. Also used in tests."""
        with self._lock:
            jobs = self.repo.jobs(state="queued")
            job = jobs[0] if jobs else None
            if job:
                session = self._session(job["session_id"])
                snapshot_id = job["snapshot_id"]
            elif self._preview:
                sid = next(iter(self._preview))
                snapshot_id = self._preview.pop(sid)
                session = self._session(sid)
            else:
                return False
            if session["state"] != "collecting":
                if job:
                    self.repo.finish_job({**job, "state": "skipped", "reason": "session_" + session["state"]})
                return True
            reference_revision = session["reference_revision"]
        snapshot = self.store.get_snapshot(snapshot_id)
        visibility, corners, candidates, error = None, None, [], None
        try:
            if job:
                self._check_snapshot(session, snapshot)
            if session["method"] == "checkerboard":
                visibility, corners, candidates = self._detect(session, snapshot)
                if job and not candidates:
                    error = "board_not_visible" if not visibility["visible"] else "board_pose_unresolved"
        except Exception as exc:
            error = type(exc).__name__ + ":" + str(exc)
        with self._lock:
            current = self._session(session["session_id"])
            if current["reference_revision"] != reference_revision:
                # Retain the queued evidence and process with the new measured size.
                return True
            if current["state"] != "collecting":
                if job:
                    self.repo.finish_job({**job, "state": "skipped", "reason": "session_" + current["state"]})
                return True
            if visibility:
                self._visibility[session["session_id"]] = visibility
                current["last_visibility"] = visibility
            if error:
                current["last_capture_error"] = error
            if job:
                sample = None
                if error is None:
                    existing = self.repo.samples(session["session_id"])
                    role = job.get("role") or ("validation" if len(existing) % 3 == 2 else "training")
                    sample = {
                        "observation_id": snapshot_id, "snapshot_id": snapshot_id,
                        "frame_id": snapshot["frame_id"], "keyframe_id": str(snapshot["keyframe_id"]),
                        "map_id": snapshot["map_id"], "calibration_id": snapshot["calibration_id"],
                        "source_map_revision": snapshot["map_revision"],
                        "T_keyframe_camera": [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]],
                        "role": role, "label": job.get("label"), "corners_px": corners,
                        "board_pose_candidates": candidates, "reference": session["reference"],
                        "reference_board_id": (session["reference"] or {}).get("reference_board_id"),
                    }
                    current["last_capture_error"] = None
                self.repo.finish_job(
                    {**job, "state": "accepted" if sample else "rejected",
                     "role": sample["role"] if sample else job.get("role"),
                     "reason": error, "finished_at": _now()}, sample)
            if error is None:
                self._error = None
            current["updated_at"] = _now()
            self.repo.save_session(current)
            if job and error is None and session["method"] == "checkerboard":
                self._fit_locked(current)
            return True

    def _run(self):
        while not self._stop.is_set():
            self._event.wait()
            self._event.clear()
            try:
                while not self._stop.is_set() and self.process_pending():
                    pass
            except Exception as exc:
                # A durable job remains queued on an unexpected worker failure.
                # Next offered frame or explicit capture wakes it; errors are visible.
                self._error = type(exc).__name__ + ":" + str(exc)

    def update_reference(self, session_id, value):
        with self._lock:
            session = self._session(session_id)
            self._editable(session)
            if session["method"] != "checkerboard":
                raise ValueError("reference_only_applies_to_checkerboard")
            reference = _reference(value, session["reference"]["reference_board_id"])
            if reference["inner_corners"] != session["reference"]["inner_corners"]:
                raise ValueError("reference_update_cannot_change_corner_pattern")
            calibration = self.store.get_calibration(session["calibration_id"])["calibration"]
            samples = self.repo.samples(session_id)
            for sample in samples:
                sample["board_pose_candidates"] = board_pose_candidates(
                    sample["corners_px"], calibration, reference)
                sample["reference"] = reference
            session.update(reference=reference, reference_revision=session["reference_revision"]+1,
                           state="collecting", candidate=None, updated_at=_now())
            self.repo.update_reference(session, samples)
            self._fit_locked(session)
            self._event.set()
            return self.status(session_id)

    def _estimate(self, session, observations):
        if session["method"] == "checkerboard":
            return estimate_metric_attachment(
                observations, validation_sample_ids=[
                    o["observation_id"] for o in observations if o["role"] == "validation"],
                policy=session["policy"])
        endpoints = {o["label"]: o for o in observations}
        needed = ["start", "end", "validation_start", "validation_end"]
        missing = [name for name in needed if name not in endpoints]
        args = session.get("fit_inputs", {})
        missing += [name for name in ("distance_m", "validation_distance_m") if name not in args]
        if missing:
            return {"qualified": False, "status": "rejected", "scale_m_per_map_unit": None,
                    "R_board_map": None, "t_board_map_m": None,
                    "failed_checks": ["missing_" + name for name in missing], "quality": {},
                    "training_observation_ids": [], "validation_observation_ids": []}
        return estimate_displacement_scale(
            endpoints["start"], endpoints["end"], measured_distance_m=args["distance_m"],
            validation_measurements=[{
                "start_observation": endpoints["validation_start"],
                "end_observation": endpoints["validation_end"],
                "measured_distance_m": args["validation_distance_m"]}],
            policy=session["policy"])

    def _resolved_candidate(self, session, samples, revision=None):
        info, observations, missing, excluded = self.repo.resolve_keyframes_for_fit(
            session["map_id"], samples, revision, exclude_deleted=session["method"] == "checkerboard")
        if info["metadata"].get("calibration_id") != session["calibration_id"]:
            missing.append("map_calibration_mismatch")
        if missing:
            candidate = {"qualified": False, "status": "stale", "scale_m_per_map_unit": None,
                         "failed_checks": ["reference_keyframe_unavailable_or_mismatched"],
                         "unresolved_observation_ids": missing, "quality": {}}
        else:
            candidate = self._estimate(session, observations)
        candidate["anchor_resolution"] = {
            "resolved_map_revision": info["revision"], "usable_observations": len(observations),
            "usable_training": sum(o["role"] == "training" for o in observations),
            "usable_validation": sum(o["role"] == "validation" for o in observations),
            "excluded_observations": excluded, "unresolved_observation_ids": list(missing),
        }
        candidate.update(map_id=session["map_id"], calibration_id=session["calibration_id"],
                         resolved_map_revision=info["revision"])
        candidate["anchor_fingerprint"] = _fingerprint(
            [{"observation_id": o["observation_id"], "T_map_camera": o.get("T_map_camera"),
              "frame_id": o["frame_id"], "keyframe_id": o["keyframe_id"]} for o in observations])
        return candidate, observations

    def _fit_locked(self, session):
        samples = self.repo.samples(session["session_id"])
        candidate, observations = self._resolved_candidate(session, samples)
        state = "ready" if candidate.get("qualified") else (
            "provisional" if candidate.get("failed_checks") == ["reference_size_unverified"] else "collecting")
        session.update(candidate=candidate, state=state, updated_at=_now())
        if candidate.get("qualified") and session["auto_apply"]:
            # Attachment and applied state commit together. Before this commit,
            # persisted collecting state remains recoverable after a crash.
            self.repo.save_attachment(session, samples, candidate)
        else:
            self.repo.save_session(session)
        return candidate

    def fit(self, session_id, inputs):
        with self._lock:
            session = self._session(session_id)
            self._editable(session)
            for key in ("distance_m", "validation_distance_m"):
                if key in inputs:
                    if session["method"] != "measured_distance":
                        raise ValueError("distance_inputs_only_for_measured_distance")
                    session["fit_inputs"][key] = _positive(inputs[key], key)
            # Explicit distances survive interruption before the fitted result.
            session.update(state="collecting", candidate=None, updated_at=_now())
            self.repo.save_session(session)
            self._fit_locked(session)
            return self.status(session_id)

    def apply(self, session_id):
        with self._lock:
            session = self._session(session_id)
            if session["state"] == "applied":
                return self.attachment_status(session["map_id"], attachment_id=session["attachment_id"])
            self._editable(session)
            samples = self.repo.samples(session_id)
            candidate, _ = self._resolved_candidate(session, samples)
            if not candidate.get("qualified"):
                session.update(candidate=candidate, state="collecting", updated_at=_now())
                self.repo.save_session(session)
                return {"availability": "unavailable", "reason": "metric_reference_not_qualified",
                        "candidate": candidate, "session_id": session_id}
            session["candidate"] = candidate
            attachment = self.repo.save_attachment(session, samples, candidate)
            return self.attachment_status(session["map_id"], attachment_id=attachment["attachment_id"])

    def cancel(self, session_id):
        with self._lock:
            session = self._session(session_id)
            if session["state"] == "applied":
                raise ValueError("applied_reference_is_immutable_start_new_session_to_replace")
            session.update(state="canceled", updated_at=_now())
            self.repo.save_session(session)
            self._preview.pop(session_id, None)
            self._event.set()
            return self.status(session_id)

    def attachment_status(self, map_id, revision=None, attachment_id=None):
        with self._lock:
            attachment = self.repo.attachment(map_id, attachment_id)
            if not attachment:
                return {"availability": "unavailable", "reason": "metric_scale_not_applied",
                        "map_id": map_id, "units": "arbitrary"}
            info = self.store.read_map(map_id, revision=revision, kinds=[], limit=1)["map"]
            cache_key = (attachment["attachment_id"], info["revision"])
            cached = self._solutions.get(attachment["attachment_id"])
            if cached and cached[0] == cache_key:
                return copy.deepcopy(cached[1])
            candidate, _ = self._resolved_candidate(
                attachment["session"], attachment["observations"], info["revision"])
            result = {
                "availability": "available" if candidate.get("qualified") else "unavailable",
                "reason": None if candidate.get("qualified") else "metric_reference_stale_or_unqualified",
                "attachment_id": attachment["attachment_id"], "session_id": attachment["session_id"],
                "map_id": map_id, "map_revision": info["revision"],
                "calibration_id": attachment["calibration_id"], "units": "m",
                "coordinate_frame": "map_metric", "axes": "same_as_native_map",
                "origin": "native_map_origin", "floor_alignment": "unmeasured",
                "pose_origin": "camera_optical", "candidate": candidate,
                "scale_m_per_map_unit": candidate.get("scale_m_per_map_unit")
                    if candidate.get("qualified") else None,
                "solution_id": _fingerprint({"attachment_id": attachment["attachment_id"],
                                            "map_revision": info["revision"], "candidate": candidate}),
            }
            self._solutions[attachment["attachment_id"]] = (cache_key, result)
            return copy.deepcopy(result)

    def status(self, session_id=None, map_id=None, include_observations=False):
        with self._lock:
            session = self.repo.get_session(session_id, map_id)
            if session is None:
                return {"availability": "unavailable", "reason": "metric_session_not_started",
                        "map_id": map_id, "metric_scale": self.attachment_status(map_id)
                        if map_id else None, "motion_commands": False}
            samples = self.repo.samples(session["session_id"])
            jobs = self.repo.jobs(session["session_id"])
            visibility = copy.deepcopy(self._visibility.get(
                session["session_id"], session.get("last_visibility")))
            if visibility:
                stamp = visibility.get("receive_decode_time")
                try:
                    age = max(0.0, (datetime.now(timezone.utc)-datetime.fromisoformat(stamp)).total_seconds())
                except (TypeError, ValueError):
                    age = None
                visibility.update(receive_decode_age_s=age, assessed_at=_now())
                visibility["current_process_observation"] = session["session_id"] in self._visibility
                visibility["matches_latest_frame"] = visibility.get("frame_id") == self._latest_frame.get(session["map_id"])
            failed = (session.get("candidate") or {}).get("failed_checks", [])
            guidance = []
            if session["state"] == "collecting":
                if session["method"] == "checkerboard":
                    if not visibility or not visibility.get("visible"):
                        guidance.append("restore_checkerboard_visibility")
                    if visibility and not visibility.get("localization_valid"):
                        guidance.append("need_visual_localization_and_camera_translation")
                    guidance.append("gather_separated_exact_keyframes_with_board_fixed")
                else:
                    guidance.append("capture_measured_camera_endpoint_pairs")
            if session.get("reference") and not session["reference"]["size_verified"]:
                guidance.append("verify_physical_square_size")
            guidance.extend(failed)
            result = {
                "availability": "available", "session": copy.deepcopy(session),
                "progress": {"accepted": len(samples),
                             "training": sum(s["role"] == "training" for s in samples),
                             "validation": sum(s["role"] == "validation" for s in samples),
                             "queued": sum(j["state"] == "queued" for j in jobs),
                             "rejected": sum(j["state"] == "rejected" for j in jobs)},
                "target": visibility, "guidance": list(dict.fromkeys(guidance)),
                "latest_keyframe": self._latest_keyframe.get(session["map_id"]),
                "worker_error": self._error, "motion_commands": False,
                "metric_scale": self.attachment_status(session["map_id"]),
            }
            if include_observations:
                result.update(observations=samples, captures=jobs)
            return result

    def metric_localization(self, snapshot):
        loc = snapshot.get("localization", {})
        scale = self.attachment_status(snapshot["map_id"], snapshot["map_revision"])
        binding = {key: snapshot.get(key) for key in
                   ("snapshot_id", "frame_id", "map_id", "map_revision", "calibration_id")}
        if scale["availability"] != "available" or not loc.get("valid") or loc.get("T_map_camera") is None:
            return {**binding, "availability": "unavailable", "valid": False,
                    "T_map_camera": None, "reason": scale.get("reason") or "localization_invalid",
                    "metric_scale": scale}
        transform = copy.deepcopy(loc["T_map_camera"])
        factor = scale["scale_m_per_map_unit"]
        for axis in range(3):
            transform[axis][3] *= factor
        return {
            **binding, "availability": "available", "valid": True,
            "T_map_camera": transform, "units": "m", "coordinate_frame": "map_metric",
            "pose_origin": "camera_optical", "floor_alignment": "unmeasured",
            "attachment_id": scale["attachment_id"], "solution_id": scale["solution_id"],
            "scale_m_per_map_unit": factor, "covariance": None,
            "uncertainty_status": "joint_pose_and_scale_uncertainty_not_estimated",
            "robot_pose": None, "robot_pose_status": "camera_to_robot_extrinsics_unavailable",
        }

    @staticmethod
    def _metric_entity(entity, factor, solution):
        item = copy.deepcopy(entity)
        data = item.get("data")
        if item.get("bounds") is not None:
            item["bounds"] = [None if x is None else x*factor for x in item["bounds"]]
        if data is None:
            return item
        if item["kind"] in ("sparse_point", "keyframe"):
            for name in ("xyz", "position"):
                if data.get(name) is not None:
                    data[name] = [x*factor for x in data[name]]
            if data.get("T_map_camera") is not None:
                for axis in range(3):
                    data["T_map_camera"][axis][3] *= factor
            # Never carry an untransformed native covariance as a metric one.
            if "covariance" in data:
                data["covariance"] = None
                data["uncertainty_status"] = "metric_uncertainty_not_estimated"
            data.update(units="m", scale_status="qualified", coordinate_frame="map_metric")
        elif item["kind"] == "sonar_state":
            data["coordinate_frame"] = "sensor_ledger_metadata"
        elif item["kind"] == "semantic_observation":
            # Image boxes and tracking pixels remain image quantities. Exact
            # keyframe anchors have identity local transforms, not global points.
            data["global_positions"] = None
            data["geometry_rule"] = "resolve_exact_keyframe_in_same_metric_solution"
        else:
            raise ValueError("unsupported_metric_entity_kind:" + item["kind"])
        item["metric_solution_id"] = solution["solution_id"]
        return item

    def read_metric_map(self, inputs, default_map_id):
        cursor = inputs.get("cursor")
        if cursor is not None and not isinstance(cursor, dict):
            raise ValueError("metric_cursor_must_be_object")
        query = (cursor or {}).get("query", {})
        map_id = inputs.get("map_id", query.get("map_id", default_map_id))
        revision = inputs.get("revision", query.get("revision"))
        attachment_id = inputs.get("attachment_id", query.get("attachment_id"))
        bounds = inputs.get("bounds", query.get("bounds"))
        kinds = inputs.get("kinds", query.get("kinds"))
        if kinds is None:
            kinds = ["keyframe", "semantic_observation", "sparse_point"]
        if not isinstance(kinds, list) or not set(kinds).issubset(
                {"keyframe", "semantic_observation", "sparse_point"}):
            raise ValueError("unsupported_metric_entity_kinds")
        kinds = sorted(set(kinds))
        solution = self.attachment_status(map_id, revision, attachment_id)
        if solution["availability"] != "available":
            return {"availability": "unavailable", "reason": solution["reason"], "metric_scale": solution}
        query_key = {"map_id": map_id, "revision": solution["map_revision"],
                     "attachment_id": solution["attachment_id"], "bounds": bounds, "kinds": kinds,
                     "solution_id": solution["solution_id"]}
        if cursor is not None and cursor.get("query") != query_key:
            raise ValueError("metric_cursor_does_not_match_pinned_solution")
        factor = solution["scale_m_per_map_unit"]
        native_bounds = None
        if bounds is not None:
            if isinstance(bounds, dict):
                native_bounds = {key: None if value is None else value/factor
                                 for key, value in bounds.items()}
            else:
                native_bounds = [None if x is None else x/factor for x in bounds]
        page = self.store.read_map(
            map_id, revision=solution["map_revision"], bounds=native_bounds, kinds=kinds,
            cursor=(cursor or {}).get("native_cursor"), limit=inputs.get("limit", 500))
        page["map"] = copy.deepcopy(page["map"])
        page["map"]["native_metadata"] = page["map"]["metadata"]
        page["map"]["metadata"] = {
            **page["map"]["metadata"], "units": "m", "scale_status": "qualified",
            "coordinate_frame": "map_metric", "axes": "same_as_native_map",
            "floor_alignment": "unmeasured", "scale_source": solution["attachment_id"]}
        page["entities"] = [self._metric_entity(e, factor, solution) for e in page["entities"]]
        if page["next_cursor"] is not None:
            page["next_cursor"] = {"query": query_key, "native_cursor": page["next_cursor"]}
        return {"availability": "available", **page, "metric_scale": solution}

    def query(self, operation, inputs, *, map_id, calibration_id):
        if operation == "robot.map.scale.start":
            return self.start_session(map_id, calibration_id, inputs)
        if operation == "robot.map.scale.status":
            return self.status(inputs.get("session_id"),
                               inputs.get("map_id", None if inputs.get("session_id") else map_id),
                               inputs.get("include_observations", False))
        if operation == "robot.map.scale.capture":
            return self.capture(inputs["session_id"], inputs["snapshot_id"],
                                inputs.get("role"), inputs.get("label"))
        if operation == "robot.map.scale.fit":
            return self.fit(inputs["session_id"], inputs)
        if operation == "robot.map.scale.apply":
            return self.apply(inputs["session_id"])
        if operation == "robot.map.scale.cancel":
            return self.cancel(inputs["session_id"])
        if operation == "robot.map.scale.reference":
            return self.update_reference(inputs["session_id"], inputs["reference"])
        if operation == "robot.map.metric.read":
            return self.read_metric_map(inputs, map_id)
        raise KeyError("unsupported_metric_operation")
