"""Headless pySLAM adapter. All exported poses refer to the camera optical origin.

The pinned upstream engine owns feature tracking, bundle adjustment, loop closing
and relocalization. This module owns calibration validation, state integrity and
the transaction boundary between the engine and the persistent observation map.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
from image_preprocessing import preprocessing_policy, preprocess_slam_image

PYSLAM_COMMIT = "a4e6a31f211ac6b2f9ecf4ab628734f6194caa15"
FORMAT = "pyslam-system-state"
FORMAT_VERSION = 1


class BackendNotReady(RuntimeError):
    pass


class CheckpointError(RuntimeError):
    pass


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inverse_pose(value):
    pose = np.asarray(value, dtype=float)
    if pose.shape != (4, 4) or not np.isfinite(pose).all():
        raise ValueError("A finite 4x4 camera transform is required")
    result = np.eye(4)
    result[:3, :3] = pose[:3, :3].T
    result[:3, 3] = -result[:3, :3] @ pose[:3, 3]
    return result


def _calibration_error(calibration):
    if not isinstance(calibration, dict) or calibration.get("validated") is not True:
        return "Supply validated intrinsic calibration; mounting height and pitch do not determine K."
    if not calibration.get("calibration_id") or not calibration.get("provenance"):
        return "Calibration requires calibration_id and provenance."
    for key in ("width", "height"):
        value = calibration.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return "Calibration width and height must be positive integers."
    for key in ("fx", "fy", "cx", "cy"):
        value = calibration.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return "Calibration requires finite fx, fy, cx and cy."
    if calibration["fx"] <= 0 or calibration["fy"] <= 0:
        return "Calibrated focal lengths must be positive."
    distortion = calibration.get("distortion")
    if not isinstance(distortion, list) or len(distortion) not in (4, 5):
        return "The pinhole backend requires four or five calibrated distortion coefficients."
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in distortion):
        return "Distortion coefficients must be finite."
    return None


def _intrinsics_hash(calibration):
    keys = ("width", "height", "fx", "fy", "cx", "cy", "distortion")
    return hashlib.sha256(_canonical({k: calibration[k] for k in keys}).encode()).hexdigest()


def _keyframe_data(identifier, kid, image_id, timestamp, transform, parent, source_frames):
    transform = np.asarray(transform, dtype=float)
    if transform.shape != (4, 4) or not np.isfinite(transform).all():
        raise ValueError("Invalid keyframe pose")
    return {
        "backend_keyframe_id": int(kid),
        "backend_frame_index": int(image_id),
        "source_frame_id": source_frames.get(str(image_id)),
        "timestamp_s": float(timestamp),
        "T_map_camera": transform.tolist(),
        "position": transform[:3, 3].tolist(),
        "parent_keyframe_id": str(parent) if parent is not None else None,
        "pose_origin": "camera_optical",
        "camera_axes": "x_right_y_down_z_forward",
        "units": "arbitrary",
        "scale_status": "unobserved",
    }


def _point_data(position, bgr):
    position = np.asarray(position, dtype=float).reshape(3)
    if not np.isfinite(position).all():
        raise ValueError("Invalid sparse map point")
    rgb = None if bgr is None else np.asarray(bgr).reshape(3)[::-1].astype(int).tolist()
    return {"xyz": position.tolist(), "rgb": rgb, "units": "arbitrary", "scale_status": "unobserved"}


def _saved_entities(document, source_frames):
    state = document.get("map")
    if not isinstance(state, dict):
        raise CheckpointError("Checkpoint has no pySLAM map")
    keyframes = state.get("keyframes", [])
    points = state.get("points", [])
    if len(keyframes) < 2 or not points:
        raise CheckpointError("No initialized geometric map is available to checkpoint")
    result = {}
    for frame in keyframes:
        if frame.get("_is_bad"):
            continue
        if not frame.get("des") or not frame.get("kps"):
            raise CheckpointError("A saved keyframe is missing features or descriptors")
        identifier = str(frame["id"])
        result[("keyframe", identifier)] = _keyframe_data(
            identifier, frame["kid"], frame["img_id"], frame["timestamp"],
            _inverse_pose(frame["pose"]), frame.get("parent"), source_frames,
        )
    for point in points:
        if not point.get("_is_bad"):
            result[("sparse_point", str(point["id"]))] = _point_data(point["pt"], point.get("color"))
    return result


class SlamBackend:
    def __init__(self, config, state_dir):
        self.config = copy.deepcopy(config)
        self._active_preprocessing = preprocessing_policy(self.config.get("preprocessing"))
        self._loaded_preprocessing = None
        self._descriptor_preprocessing_history = []
        self._preprocessing_diagnostics = {"state": "not_observed"}
        self.state_dir = Path(state_dir)
        self.pyslam_root = Path(self.config.get("pyslam_root", self.state_dir / "vendor/pyslam"))
        self._lock = threading.RLock()
        self._slam = None
        self._states = None
        self._map_id = None
        self._state = "not_started"
        self._reason = None
        self._pose = None
        self._entities = {}
        self._source_frames = {}
        self._next_frame_index = 0
        self._last_timestamp = None
        self._complete_snapshot_pending = False
        self._last_loop_id = 0
        self._geometry = self._geometry_summary({}, False)
        self._localization = self._empty_localization()
        self._radio_prior = ()
        self._last_radio_prior = ()

    def _empty_localization(self, frame_id=None, timestamp_s=None):
        return {
            "status": self._state, "valid": False, "T_map_camera": None,
            "pose_origin": "camera_optical", "camera_axes": "x_right_y_down_z_forward",
            "units": "arbitrary", "scale_status": "unobserved",
            "frame_id": frame_id, "timestamp_s": timestamp_s,
            "reference_keyframe_id": None, "reason": self._reason,
        }

    @staticmethod
    def _geometry_summary(entities, complete):
        keyframes = sum(kind == "keyframe" for kind, _ in entities)
        points = sum(kind == "sparse_point" for kind, _ in entities)
        return {
            "available": keyframes >= 2 and points > 0,
            "point_count": points, "keyframe_count": keyframes,
            "units": "arbitrary", "scale_status": "unobserved",
            "complete_snapshot": complete,
        }

    def _new_runtime(self):
        if not (self.pyslam_root / "pyslam/slam/slam.py").is_file():
            raise BackendNotReady("pySLAM source is not installed; run setup_slam.py.")
        revision = subprocess.check_output(
            ["/usr/bin/git", "-C", str(self.pyslam_root), "rev-parse", "HEAD"], text=True
        ).strip()
        if revision != PYSLAM_COMMIT:
            raise BackendNotReady("pySLAM revision differs from the qualified dependency pin.")
        os.environ["PYSLAM_USE_CPP"] = "false"
        if str(self.pyslam_root) not in sys.path:
            sys.path.insert(0, str(self.pyslam_root))
        from pyslam.config import Config
        from pyslam.config_parameters import Parameters
        Config()
        Parameters.USE_CPP_CORE = False
        Parameters.kDoVolumetricIntegration = False
        Parameters.kDoSparseSemanticMappingAndSegmentation = False
        Parameters.kUseDepthEstimatorInFrontEnd = False
        Parameters.kUseLoopClosing = True
        # Full loop-keyframe images are optional headless visualization data.
        Parameters.kLoopClosingDebugWithLoopConsistencyCheckImages = False
        Parameters.kLoopClosingDebugShowLoopMatchedPoints = False
        Parameters.kOptimizationFrontEndUseGtsam = False
        Parameters.kOptimizationBundleAdjustUseGtsam = False
        Parameters.kOptimizationLoopClosingUseGtsam = False
        from pyslam.slam import Slam, PinholeCamera
        from pyslam.slam.slam_commons import SlamState
        from pyslam.io.dataset_types import SensorType, DatasetEnvironmentType
        from pyslam.local_features.feature_tracker_configs import FeatureTrackerConfigs
        from pyslam.loop_closing.loop_detector_configs import LoopDetectorConfigs
        calibration = self.config["calibration"]
        settings = {"Camera." + k: calibration[k] for k in ("width", "height", "fx", "fy", "cx", "cy")}
        settings["Camera.fps"] = float(self.config.get("fps", 5.0))
        camera = PinholeCamera(SimpleNamespace(
            cam_settings=settings, DistCoef=calibration["distortion"], sensor_type=SensorType.MONOCULAR
        ))
        tracker = copy.deepcopy(FeatureTrackerConfigs.ORB2)
        tracker["num_features"] = int(self.config.get("num_features", 2000))
        loop = copy.deepcopy(LoopDetectorConfigs.DBOW3)
        self._states = SlamState
        return Slam(camera, tracker, loop, None, SensorType.MONOCULAR,
                    environment_type=DatasetEnvironmentType.INDOOR, config=None, headless=True)

    def _checkpoint_path(self, checkpoint):
        if isinstance(checkpoint, dict):
            value = checkpoint.get("path")
            if value is None:
                if checkpoint.get("availability") in (False, "unavailable", None):
                    return None
                raise CheckpointError("Backend checkpoint has no artifact path")
            return Path(value)
        return Path(checkpoint) if checkpoint is not None else None

    @staticmethod
    def _checkpoint_preprocessing(manifest):
        if "active_preprocessing" in manifest and not isinstance(manifest["active_preprocessing"], dict):
            raise ValueError("Invalid active preprocessing profile")
        active = preprocessing_policy(manifest.get("active_preprocessing"))
        history = manifest.get("descriptor_preprocessing_history", [{"mode": "none", "version": 1}])
        if (not isinstance(history, list) or not history or
                any(not isinstance(value, dict) for value in history)):
            raise ValueError("Invalid descriptor preprocessing history")
        return active, [preprocessing_policy(value) for value in history]

    def _validate_checkpoint(self, path):
        manifest_path = path / "backend_manifest.json"
        if not manifest_path.is_file():
            raise CheckpointError("Missing backend checkpoint manifest")
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("format") != FORMAT or manifest.get("format_version") != FORMAT_VERSION:
            raise CheckpointError("Unsupported backend checkpoint format")
        if manifest.get("pyslam_commit") != PYSLAM_COMMIT or manifest.get("map_id") != self._map_id:
            raise CheckpointError("Checkpoint engine revision or map identity mismatch")
        if manifest.get("intrinsics_sha256") != _intrinsics_hash(self.config["calibration"]):
            raise CheckpointError("Checkpoint intrinsics differ from the active calibration")
        for entry in manifest.get("files", []):
            name = Path(entry["path"])
            resolved = (path / name).resolve()
            if name.is_absolute() or not resolved.is_relative_to(path.resolve()):
                raise CheckpointError("Checkpoint file escapes its package")
            if not resolved.is_file() or resolved.stat().st_size != entry["size"] or _sha_file(resolved) != entry["sha256"]:
                raise CheckpointError("Checkpoint integrity failed: " + str(name))
        required = {"map.json", "config_info.json", "loop_closing.db"}
        if not required.issubset({entry["path"] for entry in manifest.get("files", [])}):
            raise CheckpointError("Checkpoint is missing map or loop database files")
        try:
            self._checkpoint_preprocessing(manifest)
        except ValueError as exc:
            raise CheckpointError("Invalid checkpoint preprocessing metadata") from exc
        document = json.loads((path / "map.json").read_text())
        entities = _saved_entities(document, manifest.get("source_frames", {}))
        next_index = manifest.get("next_frame_index")
        saved_indices = [value["backend_frame_index"] for (kind, _), value in entities.items()
                         if kind == "keyframe"]
        saved_indices.extend(int(frame["img_id"]) for frame in document["map"].get("frames", []))
        if isinstance(next_index, bool) or not isinstance(next_index, int) or next_index <= max(saved_indices):
            raise CheckpointError("Checkpoint next_frame_index must exceed every saved frame index")
        return manifest

    def start(self, map_id, checkpoint=None):
        with self._lock:
            if self._slam is not None:
                raise RuntimeError("Close the existing backend before starting another map")
            self._map_id = str(map_id)
            self._radio_prior = self._last_radio_prior = ()
            self._entities = {}
            self._source_frames = {}
            self._next_frame_index = 0
            self._geometry = self._geometry_summary({}, False)
            self._complete_snapshot_pending = False
            self._reason = _calibration_error(self.config.get("calibration"))
            if self._reason:
                self._state = "awaiting_calibration"
                self._localization = self._empty_localization()
                return self.status()
            path = self._checkpoint_path(checkpoint)
            manifest = self._validate_checkpoint(path) if path is not None else None
            self._loaded_preprocessing, self._descriptor_preprocessing_history = (
                self._checkpoint_preprocessing(manifest) if manifest is not None else (None, []))
            self._preprocessing_diagnostics = {"state": "not_observed"}
            try:
                self._slam = self._new_runtime()
                if manifest is not None:
                    self._slam.load_system_state(str(path))
                    self._source_frames = manifest.get("source_frames", {})
                    self._next_frame_index = int(manifest["next_frame_index"])
                    self._slam.set_tracking_state(self._states.INIT_RELOCALIZE)
                    self._state = "relocalizing"
                    self._reason = "Restored map; a fresh frame must relocalize before a current pose is valid."
                else:
                    self._state = "initializing"
                    self._reason = "Waiting for sufficient camera motion and static visual features."
                self._last_loop_id = int(self._slam.loop_closing.last_loop_kf_id)
                self._complete_snapshot_pending = True
                self._last_timestamp = None
                self._entities = {}
                self._localization = self._empty_localization()
            except (Exception, SystemExit) as exc:
                self._state = "error"
                self._reason = str(exc)
                self._localization = self._empty_localization()
                if self._slam is not None:
                    self._slam.quit()
                    self._slam = None
                raise BackendNotReady(self._reason) from exc
            return self.status()

    def _read_entities(self):
        entities = {}
        for keyframe in self._slam.map.get_keyframes():
            if not keyframe.is_bad():
                identifier = str(keyframe.id)
                parent = keyframe.parent.id if keyframe.parent is not None else None
                entities[("keyframe", identifier)] = _keyframe_data(
                    identifier, keyframe.kid, keyframe.img_id, keyframe.timestamp,
                    keyframe.Twc(), parent, self._source_frames,
                )
        for point in self._slam.map.get_points():
            if not point.is_bad():
                entities[("sparse_point", str(point.id))] = _point_data(point.pt(), point.color)
        return entities

    def _diff(self, entities, complete=False):
        changes = []
        for key in sorted(entities):
            if complete or key not in self._entities or entities[key] != self._entities[key]:
                changes.append({"op": "upsert", "kind": key[0], "entity_id": key[1], "data": entities[key]})
        for key in sorted(self._entities.keys() - entities.keys()):
            changes.append({"op": "delete", "kind": key[0], "entity_id": key[1], "data": {}})
        self._entities = copy.deepcopy(entities)
        self._geometry = self._geometry_summary(entities, complete)
        return changes

    def _current_pose(self):
        tracking = self._slam.tracking
        history = tracking.tracking_history
        frame = tracking.f_cur
        if not history.ids or history.ids[-1] != frame.id:
            return None, None
        reference = history.kf_references[-1]
        relative = np.asarray(history.relative_frame_poses[-1].matrix(), dtype=float)
        chain = np.eye(4)
        seen = set()
        while reference.is_bad():
            if reference.parent is None or reference.id in seen:
                raise ValueError("Current pose has no surviving keyframe anchor")
            seen.add(reference.id)
            chain = chain @ reference.Tcp()
            reference = reference.parent
        # Saved relative poses are T_current_reference. Reapply corrected map
        # keyframe poses after bundle adjustment or a loop closure.
        transform = _inverse_pose(relative @ chain @ reference.Tcw())
        return transform, str(reference.id)

    def set_relocalization_prior(self, keyframe_ids, *, map_id):
        """A bounded candidate hint, never a camera pose or acceptance decision."""
        with self._lock:
            selected = []
            if str(map_id) == self._map_id:
                for identifier in keyframe_ids:
                    if isinstance(identifier, bool):
                        continue
                    try:
                        value = int(identifier)
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if value < 0 or str(value) != str(identifier) or value in selected:
                        continue
                    selected.append(value)
                    if len(selected) == 128:
                        break
            self._radio_prior = tuple(selected)

    def track(self, image_bgr, frame_id, timestamp_s):
        with self._lock:
            if self._slam is None:
                self._localization = self._empty_localization(frame_id, timestamp_s)
                return {"localization": copy.deepcopy(self._localization),
                        "geometry": dict(self._geometry, complete_snapshot=False), "map_changes": [],
                        "map_change_reason": "mapping", "is_keyframe": False,
                        "new_keyframe_id": None, "observation_anchor": None}
            timestamp_s = float(timestamp_s)
            if not math.isfinite(timestamp_s):
                raise ValueError("Frame timestamp must be finite")
            if self._last_timestamp is not None and timestamp_s <= self._last_timestamp:
                self.invalidate_tracking("Nonmonotonic frame timestamp")
                raise ValueError("Frame timestamps must increase within a stream session")
            calibration = self.config["calibration"]
            image = np.asarray(image_bgr)
            if image.shape != (calibration["height"], calibration["width"], 3) or image.dtype != np.uint8:
                self.invalidate_tracking("Image dimensions or dtype do not match calibration")
                raise ValueError("Expected calibrated-size uint8 BGR image")
            index = self._next_frame_index
            self._next_frame_index += 1
            self._source_frames[str(index)] = frame_id
            self._last_timestamp = timestamp_s
            try:
                # Exported geometry can be empty immediately after restore. Native
                # identity, rather than first export, determines a new keyframe.
                with self._slam.map.update_lock:
                    known_keyframe_ids = {str(keyframe.id) for keyframe in self._slam.map.get_keyframes()}
                image, diagnostics = preprocess_slam_image(image, self._active_preprocessing)
                self._preprocessing_diagnostics = {**diagnostics, "frame_id": frame_id}
                loop = self._slam.loop_closing
                self._last_radio_prior = tuple(
                    identifier for identifier in self._radio_prior
                    if str(identifier) in known_keyframe_ids)[:6]
                if getattr(loop, "radio_relocalization_prior_version", 0) >= 1:
                    loop.relocalization_prior_frame_ids = self._last_radio_prior
                self._radio_prior = ()
                self._slam.track(image, None, None, index, timestamp_s)
                native_state = self._slam.tracking.state.name
                self._state = {"OK": "tracking", "LOST": "lost", "RELOCALIZE": "relocalizing",
                               "INIT_RELOCALIZE": "relocalizing"}.get(native_state, "initializing")
                self._reason = None
                self._localization = self._empty_localization(frame_id, timestamp_s)
                with self._slam.map.update_lock:
                    entities = self._read_entities()
                    if (any(kind == "keyframe" and identifier not in known_keyframe_ids
                            for kind, identifier in entities) and
                            self._active_preprocessing not in self._descriptor_preprocessing_history):
                        self._descriptor_preprocessing_history.append(copy.deepcopy(self._active_preprocessing))
                    if native_state == "OK":
                        transform, reference = self._current_pose()
                        if transform is not None:
                            self._localization.update(valid=True, T_map_camera=transform.tolist(),
                                                      reference_keyframe_id=reference)
                        else:
                            self._state = "initializing"
                            self._reason = "Waiting for the first fresh tracked pose relative to a map keyframe."
                            self._localization = self._empty_localization(frame_id, timestamp_s)
                    complete = self._complete_snapshot_pending
                    changes = self._diff(entities, complete)
                    self._complete_snapshot_pending = False
                    new_keyframe = next((identifier for (kind, identifier), value in entities.items()
                                         if kind == "keyframe" and value["backend_frame_index"] == index
                                         and identifier not in known_keyframe_ids), None)
                loop_id = int(self._slam.loop_closing.last_loop_kf_id)
                reason = "loop_closure" if loop_id != self._last_loop_id else "mapping"
                self._last_loop_id = loop_id
                anchor = None
                if new_keyframe is not None and self._localization["valid"]:
                    anchor = {"keyframe_id": new_keyframe, "source_frame_id": frame_id,
                              "T_keyframe_camera": np.eye(4).tolist()}
                return {"localization": copy.deepcopy(self._localization), "geometry": copy.deepcopy(self._geometry),
                        "map_changes": changes, "map_change_reason": reason,
                        "is_keyframe": anchor is not None, "new_keyframe_id": new_keyframe,
                        "observation_anchor": anchor}
            except Exception as exc:
                self.invalidate_tracking("Tracking error: " + str(exc))
                raise

    def invalidate_tracking(self, reason):
        with self._lock:
            self._reason = str(reason)
            self._last_timestamp = None
            if self._slam is not None:
                if self._slam.map.num_keyframes() >= 2:
                    self._slam.set_tracking_state(self._states.INIT_RELOCALIZE)
                    self._state = "relocalizing"
                else:
                    self._state = "initializing"
                self._slam.tracking.cur_R = None
                self._slam.tracking.cur_t = None
                self._slam.tracking.motion_model.reset()
            self._localization = self._empty_localization()
            return self.status()

    def checkpoint(self, target_dir):
        with self._lock:
            if self._slam is None or self._slam.map.num_keyframes() < 2 or self._slam.map.num_points() == 0:
                raise BackendNotReady("A real initialized SLAM map is required for a backend checkpoint")
            target = Path(target_dir)
            if target.exists() and any(target.iterdir()):
                raise CheckpointError("Checkpoint target must be a fresh empty directory")
            target.mkdir(parents=True, exist_ok=True)
            self._slam.save_system_state(str(target))
            # Upstream can catch a serialization exception internally. Verify
            # actual files and parse the saved map before reporting success.
            document = json.loads((target / "map.json").read_text())
            entities = _saved_entities(document, self._source_frames)
            if not (target / "loop_closing.db").is_file():
                raise CheckpointError("Loop database was not saved")
            files = [{"path": str(path.relative_to(target)), "size": path.stat().st_size,
                      "sha256": _sha_file(path)}
                     for path in sorted(target.rglob("*")) if path.is_file()]
            used_indices = {str(value["backend_frame_index"]) for (kind, _), value in entities.items()
                            if kind == "keyframe"}
            manifest = {
                "format": FORMAT, "format_version": FORMAT_VERSION, "pyslam_commit": PYSLAM_COMMIT,
                "map_id": self._map_id, "intrinsics_sha256": _intrinsics_hash(self.config["calibration"]),
                "calibration": self.config["calibration"], "mounting": self.config.get("mounting"),
                "active_preprocessing": copy.deepcopy(self._active_preprocessing),
                "descriptor_preprocessing_history": copy.deepcopy(
                    self._descriptor_preprocessing_history or [self._active_preprocessing]),
                "next_frame_index": self._next_frame_index,
                "source_frames": {k: self._source_frames[k] for k in used_indices if k in self._source_frames},
                "units": "arbitrary", "scale_status": "unobserved", "files": files,
                "created_at_unix_s": time.time(), "geometry_source": "saved_map_json",
            }
            temporary = target / "backend_manifest.json.tmp"
            with temporary.open("w") as handle:
                handle.write(_canonical(manifest))
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(target / "backend_manifest.json")
            changes = self._diff(entities, complete=True)
            return {**manifest, "availability": "available", "path": str(target),
                    "geometry": copy.deepcopy(self._geometry), "map_changes": changes,
                    "map_change_reason": "checkpoint", "exact_saved_geometry": True}

    def status(self):
        with self._lock:
            return {"backend": "pyslam", "pyslam_commit": PYSLAM_COMMIT, "runtime_loaded": self._slam is not None,
                    "status": self._state, "reason": self._reason, "map_id": self._map_id,
                    "localization": copy.deepcopy(self._localization), "geometry": copy.deepcopy(self._geometry),
                    "calibration": copy.deepcopy(self.config.get("calibration")),
                    "mounting": copy.deepcopy(self.config.get("mounting")),
                    "robot_extrinsics_available": False,
                    "radio_prior": {
                        "supported": self._slam is not None and getattr(
                            getattr(self._slam, "loop_closing", None), "radio_relocalization_prior_version", 0) >= 1,
                        "candidate_frame_ids": list(self._last_radio_prior),
                        "last_native_result": copy.deepcopy(getattr(
                            getattr(self._slam, "loop_closing", None), "last_radio_relocalization", None)) if self._slam else None,
                        "acceptance": "unchanged_visual_geometry_with_full_visual_fallback",
                    },
                    "preprocessing": {
                        "active": copy.deepcopy(self._active_preprocessing),
                        "loaded": copy.deepcopy(self._loaded_preprocessing),
                        "descriptor_history": copy.deepcopy(self._descriptor_preprocessing_history),
                        "history_scope": "profiles_that_have_contributed_keyframe_descriptors",
                        "point_colors": "from_keyframe_inputs_may_include_grayscale",
                        "last_frame": copy.deepcopy(self._preprocessing_diagnostics),
                    }}

    def close(self):
        with self._lock:
            if self._slam is not None:
                self._slam.quit()
                self._slam = None
            self._state = "closed"
            self._reason = None
            self._localization = self._empty_localization()
