"""Motion-compensated FTM/visual frame registration; never navigation authority.

Raw ranges retain their Mac monotonic receive-time estimates. Visual samples are
bound to native keyframes so optimization cannot silently reuse old coordinates.
Only a validated similarity and a separately observed signed floor are exposed.
"""
import copy
import itertools
import json
import math
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation


def point(value):
    p = np.asarray(value, float)
    if p.shape != (3,) or not np.isfinite(p).all():
        raise ValueError("invalid_point")
    return p


def matrix(value):
    t = np.asarray(value, float)
    if t.shape != (4, 4) or not np.isfinite(t).all() or not np.allclose(t[3], [0, 0, 0, 1]):
        raise ValueError("invalid_transform")
    return t


def transform(t, p):
    return np.asarray(p) @ t[:3, :3].T + t[:3, 3]


def similarity(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    x, y = a-a.mean(0), b-b.mean(0)
    if len(a) < 3 or np.linalg.matrix_rank(x, tol=1e-7) < 2 or np.linalg.matrix_rank(y, tol=1e-7) < 2:
        raise ValueError("motion_not_two_dimensional")
    u, s, vt = np.linalg.svd(y.T @ x / len(a))
    sign = np.ones(3); sign[-1] = np.linalg.det(u @ vt)
    r = (u * sign) @ vt
    scale = float(s @ sign / np.mean(np.sum(x*x, axis=1)))
    if not np.isfinite(scale) or scale <= 0:
        raise ValueError("invalid_scale")
    t = np.eye(4); t[:3, :3] = scale*r; t[:3, 3] = b.mean(0)-scale*r @ a.mean(0)
    return t


def rotations():
    for permutation in itertools.permutations(range(3)):
        for signs in itertools.product((-1., 1.), repeat=3):
            r = np.eye(3)[:, permutation] * np.array(signs)
            if np.linalg.det(r) > 0:
                yield r


def fit_ranges(rows, anchors, prior=None):
    """Fit independent range sessions at their interpolated visual positions.

    Held-out sessions never enter the optimizer. Distinct low-error spatial
    solutions, weak motion, poor residuals and singular information stay invalid.
    """
    if len(rows) < 30:
        return {"valid": False, "status": "awaiting_range_motion_samples", "sample_count": len(rows)}
    groups = {}
    for row in rows:
        groups.setdefault(row["anchor"], []).append(row)
    groups = {k: sorted(v, key=lambda r: r["time_s"]) for k, v in groups.items() if len(v) >= 5 and k in anchors}
    if len(groups) < 4:
        return {"valid": False, "status": "awaiting_four_ranged_anchors"}
    anchor_points = np.array([anchors[k] for k in groups])
    if np.linalg.matrix_rank(anchor_points-anchor_points.mean(0), tol=1e-5) < 3:
        return {"valid": False, "status": "anchor_mirror_ambiguity"}
    train, validation = [], []
    for group in groups.values():
        for i, row in enumerate(group):
            (validation if i % 5 == 4 else train).append(row)
    p = np.array([r["point"] for r in train]); a = np.array([anchors[r["anchor"]] for r in train])
    distances = np.array([r["distance_m"] for r in train])
    vp = np.array([r["point"] for r in validation]); va = np.array([anchors[r["anchor"]] for r in validation])
    vd = np.array([r["distance_m"] for r in validation])
    centered = p-p.mean(0)
    singular = np.linalg.svd(centered, compute_uv=False)
    if len(singular) < 2 or singular[1] < max(1e-6, singular[0]*.05):
        return {"valid": False, "status": "awaiting_turning_motion"}
    # Estimate scale from independent within-anchor range variation, then search
    # all proper signed-axis rotations; there is no assumed floor/world rotation.
    spread = np.median([np.std([r["distance_m"] for r in g]) for g in groups.values()])
    scale_guess = max(.01, float(spread/max(np.sqrt(np.mean(centered**2)), 1e-6)))
    def unpack(x):
        r = Rotation.from_rotvec(x[:3]).as_matrix(); scale = np.exp(x[6])
        t = np.eye(4); t[:3, :3] = scale*r; t[:3, 3] = x[3:6]
        return t
    def residual(x):
        return np.linalg.norm(transform(unpack(x), p)-a, axis=1)-distances
    starts = []
    if prior is not None:
        scale = np.cbrt(np.linalg.det(prior[:3, :3]))
        starts.append(np.r_[Rotation.from_matrix(prior[:3, :3]/scale).as_rotvec(), prior[:3, 3], np.log(scale)])
    for r in rotations():
        starts.append(np.r_[Rotation.from_matrix(r).as_rotvec(), anchor_points.mean(0)-scale_guess*r @ p.mean(0), np.log(scale_guess)])
    candidates = []
    for start in starts:
        fit = least_squares(residual, start, loss="soft_l1", f_scale=.3,
                            bounds=(np.r_[[-np.inf]*6, -12.], np.r_[[np.inf]*6, 12.]), max_nfev=250)
        if fit.success and np.isfinite(fit.x).all():
            candidates.append((float(np.mean(residual(fit.x)**2)), fit, unpack(fit.x)))
    if not candidates:
        return {"valid": False, "status": "range_fit_failed"}
    candidates.sort(key=lambda c: c[0]); _, fit, t = candidates[0]
    errors = residual(fit.x); validation_errors = np.linalg.norm(transform(t, vp)-va, axis=1)-vd
    rms = float(np.sqrt(np.mean(errors**2))); vrms = float(np.sqrt(np.mean(validation_errors**2)))
    threshold = max(.75, .15*float(np.median(distances)))
    scale = float(np.exp(fit.x[6])); spans = singular[:2]*scale/math.sqrt(len(p))
    jac = fit.jac; norms = np.linalg.norm(jac, axis=0)
    information = np.linalg.svd(jac/np.maximum(norms, 1e-12), compute_uv=False)
    condition = float(information[0]/max(information[-1], 1e-12))
    result = {"valid": False, "status": "range_fit_inconsistent", "sample_count": len(train)+len(validation),
              "training_count": len(train), "validation_count": len(validation), "anchor_count": len(groups),
              "training_rms_m": rms, "validation_rms_m": vrms, "acceptance_m": threshold,
              "motion_spread_m": spans.tolist(), "information_condition": condition,
              "per_anchor_rms_m": {k: float(np.sqrt(np.mean([e*e for e, r in zip(errors, train) if r["anchor"] == k]))) for k in groups},
              "accuracy": "fit_residuals_are_not_calibrated_RF_accuracy"}
    if spans[0] < .5 or spans[1] < .25:
        result["status"] = "awaiting_larger_turning_motion"; return result
    if condition > 1e5:
        result["status"] = "weak_registration_geometry"; return result
    if rms > threshold or vrms > threshold or np.mean(np.abs(validation_errors) <= threshold*2) < .9:
        return result
    for score, _, alternative in candidates[1:]:
        if math.sqrt(score) <= rms+.05 and np.sqrt(np.mean(np.sum((transform(t, p)-transform(alternative, p))**2, axis=1))) > max(.5, threshold):
            result["status"] = "ambiguous_registration"; return result
    result.update(valid=True, status="range_motion_registered", T_anchor_map=t.tolist(), scale_m_per_map_unit=scale)
    return result


def floor_from_motion(positions, first_rotation, camera_height_m, up=None, floor_order=None):
    p = np.asarray(positions, float)
    if len(p) < 8:
        return {"valid": False, "status": "awaiting_floor_motion"}
    center = np.median(p, axis=0); _, s, vt = np.linalg.svd(p-center, full_matrices=False)
    if s[1]/math.sqrt(len(p)) < .25:
        return {"valid": False, "status": "awaiting_turning_motion"}
    normal = vt[-1]; offsets = (p-center) @ normal
    inliers = np.abs(offsets-np.median(offsets)) <= .12
    if np.mean(inliers) < .9:
        return {"valid": False, "status": "motion_not_on_one_floor"}
    _, _, vt = np.linalg.svd(p[inliers]-p[inliers].mean(0), full_matrices=False); normal = vt[-1]
    source = None
    if up is not None:
        up = point(up); up /= np.linalg.norm(up)
        if abs(float(normal @ up)) < math.cos(math.radians(15)):
            return {"valid": False, "status": "floor_gravity_disagreement"}
        if normal @ up < 0: normal = -normal
        source = "matched_mounted_accelerometer"
    elif floor_order is not None and abs(float(normal @ point(floor_order))) > .5:
        if normal @ point(floor_order) < 0: normal = -normal
        source = "owner_floor_group_order_not_equal_anchor_heights"
    if source is None:
        return {"valid": False, "status": "floor_normal_sign_unobserved", "unsigned_normal": normal.tolist()}
    forward = np.asarray(first_rotation)[:, 2]; forward = forward-normal*float(forward @ normal)
    if np.linalg.norm(forward) < .1:
        return {"valid": False, "status": "floor_heading_unobserved"}
    forward /= np.linalg.norm(forward); right = np.cross(normal, forward); right /= np.linalg.norm(right)
    forward = np.cross(right, normal)
    rotation = np.array([right, normal, forward]); origin = p[0]-normal*camera_height_m
    origin = p[0]-normal*(float((p[0]-p[inliers].mean(0)) @ normal)+camera_height_m)
    t = np.eye(4); t[:3, :3] = rotation; t[:3, 3] = -rotation @ origin
    return {"valid": True, "status": "estimated_floor", "normal_anchor": normal.tolist(),
            "T_floor_anchor": t.tolist(), "camera_height_m": camera_height_m, "sign_source": source,
            "plane_rms_m": float(np.sqrt(np.mean(((p[inliers]-origin) @ normal-camera_height_m)**2))),
            "qualification": "level_rover_motion_and_owner_camera_height_not_surveyed_floor"}


class FrameRegistration:
    def __init__(self, path, resolve_anchor, config=None):
        self.path = Path(path); self.resolve_anchor = resolve_anchor; self.config = config or {}
        self.lock = threading.RLock(); self.poses = deque(maxlen=4500); self.rows = deque(maxlen=600)
        self.seen = {}; self.attachments = {}; self.binding = None; self.baseline = None; self.last_pose = None
        self.generation = 0; self.future = None; self.last_fit = -1e30; self.last_save = -1e30
        self.rejected_fit = False; self.last_fit_key = None
        self.stream_cache = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tof-registration")
        self.diagnostic = {"valid": False, "status": "awaiting_matched_motion", "sample_count": 0}
        self.floor_priors = {}
        priors = self.path.parent / "tof-floor-priors.json"
        if priors.exists():
            self.floor_priors = json.loads(priors.read_text()).get("anchors", {})
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                if data.get("schema") == "rover.tof.registration/1":
                    self.attachments = data.get("attachments", {})
            except (OSError, ValueError, TypeError):
                self.diagnostic["load_error"] = "registration_state_unreadable"

    def _base(self, tof, map_id):
        b = tof.get("baseline") or {}
        identity = (map_id, b.get("frame_id"), b.get("epoch"))
        if identity != self.binding:
            self.binding = identity; self.generation += 1; self.rows.clear(); self.poses.clear(); self.seen.clear(); self.last_pose = None
            self.diagnostic = {"valid": False, "status": "awaiting_matched_motion", "sample_count": 0}
            self.rejected_fit = False
            self.last_fit_key = None
            self.stream_cache = None
        self.baseline = copy.deepcopy(b)
        return b

    def observe(self, frame, localization, tof, keyframe, attitude=None, mount=None):
        now = time.monotonic(); map_id = localization.get("map_id")
        with self.lock:
            b = tof.get("baseline") or {}
            if not b.get("valid") or float(tof.get("collector_age_s", math.inf)) > 5:
                self.diagnostic = {"valid": False, "status": "baseline_unavailable"}; return
            self._base(tof, map_id)
            if not localization.get("valid") or not keyframe:
                self.last_pose = None; self.poses.clear()
                self.stream_cache = None
                self.diagnostic.update(status="awaiting_visual_tracking_and_keyframe"); return
            try:
                capture = frame.get("capture_host_monotonic_interval_s")
                host_time = float(frame["receive_decode_monotonic_s"])
                if capture is not None:
                    low, high = map(float, capture)
                    if not (math.isfinite(low) and low <= high <= host_time):
                        return
                    # Bound the exposure on the host clock using this capture request.
                    host_time = (low + high) / 2
                if not 0 <= now-host_time <= 3: return
                camera = matrix(localization["T_map_camera"]); anchor = matrix(keyframe["T_map_keyframe"])
                pose = {"time_s": host_time, "anchor_id": str(keyframe["id"]),
                        "T_key_camera": (np.linalg.inv(anchor) @ camera).tolist(), "frame_id": frame["frame_id"],
                        "floor_eligible": frame.get("tof_floor_motion_eligible", True)}
                if attitude is not None and mount is not None:
                    pose["R_ref_imu"] = np.asarray(attitude["R"]).tolist(); pose["imu_epoch"] = list(attitude["epoch"])
                    pose["R_camera_imu"] = np.asarray(mount).tolist()
                    sensor_state = (((frame.get("sensor_packet") or {}).get("ahrs") or {}).get("ahrs") or {})
                    imu = sensor_state.get("imu") or {}
                    acceleration = np.asarray(imu.get("accel_m_s2", []), float)
                    if acceleration.shape == (3,) and np.isfinite(acceleration).all() and abs(np.linalg.norm(acceleration)-9.81) < .6:
                        pose["up_camera"] = (np.asarray(mount) @ (acceleration/np.linalg.norm(acceleration))).tolist()
            except (ValueError, KeyError, TypeError): return
            if not self.poses or host_time > self.poses[-1]["time_s"]:
                self.poses.append(pose)
            self.last_pose = pose
            history = list(self.poses)
            for raw in tof.get("rover_ranges", []):
                try:
                    target = str(raw["target_bssid"]).lower(); observed = float(raw["observed"])
                    if raw.get("consistent") is False: continue
                    distance = float(raw.get("corrected_distance_m", raw["distance_m"]))
                    if not np.isfinite([observed, distance]).all() or distance <= 0 or not 0 <= now-observed <= 30 or raw.get("samples", 0) <= 0: continue
                    signature = (distance, raw.get("rtt_ns"), raw.get("samples"))
                    old = self.seen.get(target)
                    if old and observed <= old[0]+(1. if old[1] == signature else .05): continue
                    bracket = next(((a, z) for a, z in zip(history, history[1:]) if a["time_s"] <= observed <= z["time_s"] and 0 < z["time_s"]-a["time_s"] <= .75), None)
                    if bracket is None: continue
                    a, z = bracket; f = (observed-a["time_s"])/(z["time_s"]-a["time_s"])
                    self.rows.append({"anchor": target, "time_s": observed, "distance_m": distance,
                                      "left": copy.deepcopy(a), "right": copy.deepcopy(z), "fraction": f})
                    self.seen[target] = (observed, signature)
                except (ValueError, KeyError, TypeError): continue
            self.diagnostic["sample_count"] = len(self.rows)
            fit_key = (len(self.rows), self.rows[-1]["time_s"] if self.rows else None, b.get("revision"))
            if now-self.last_fit >= 5 and self.future is None and len(self.rows) >= 30 and fit_key != self.last_fit_key:
                self.last_fit = now
                self.last_fit_key = fit_key
                args = (self.generation, copy.deepcopy(b), map_id, localization["map_revision"], copy.deepcopy(list(self.rows)))
                self.future = self.executor.submit(self._fit, *args)
                self.future.add_done_callback(self._finished)

    def _resolved(self, pose, map_id, revision, controls):
        identifier = pose["anchor_id"]
        if identifier not in controls:
            value = self.resolve_anchor(map_id, identifier, revision)
            if value is None: raise ValueError("keyframe_unavailable")
            controls[identifier] = matrix(value["T_map_keyframe"])
        return controls[identifier] @ matrix(pose["T_key_camera"])

    def _fit(self, generation, baseline, map_id, revision, rows):
        controls = {}; resolved = []; orientations = []; ups = []; camera_positions = []; first_rotation = None
        for row in rows:
            try:
                a = self._resolved(row["left"], map_id, revision, controls); z = self._resolved(row["right"], map_id, revision, controls)
                position = a[:3, 3]*(1-row["fraction"])+z[:3, 3]*row["fraction"]
                resolved.append({**row, "point": position.tolist()})
                if row["left"].get("floor_eligible", True) and row["right"].get("floor_eligible", True):
                    camera_positions.append(position)
                first_rotation = a[:3, :3] if first_rotation is None else first_rotation
                sample = row["left"]
                if "up_camera" in sample and sample.get("floor_eligible", True): ups.append(a[:3, :3] @ point(sample["up_camera"]))
                if "R_ref_imu" in sample:
                    orientations.append((sample["imu_epoch"], a[:3, :3] @ np.array(sample["R_camera_imu"]) @ np.array(sample["R_ref_imu"]).T))
            except (ValueError, KeyError, TypeError): continue
        anchors = {p["bssid"].lower(): point([p["x_m"], p["y_m"], p["z_m"]]) for p in baseline["positions"]}
        prior = self.attachments.get(map_id)
        prior_t = matrix(prior["T_anchor_map"]) if prior and prior.get("frame_id") == baseline["frame_id"] else None
        # A qualified Sim(3) must survive future keyframe optimization.
        # Check its required independent controls before running 24 nonlinear fits.
        xyz = np.array([control[:3, 3] for control in controls.values()])
        observable = len(xyz) >= 3 and np.linalg.matrix_rank(xyz-xyz.mean(0), tol=1e-6) >= 2
        fitted = (fit_ranges(resolved, anchors, prior_t) if observable else
                  {"valid": False, "status": "awaiting_observable_keyframe_controls",
                   "sample_count": len(resolved), "control_keyframe_count": len(controls)})
        fitted.update(map_id=map_id, map_revision=revision, frame_id=baseline["frame_id"],
                      baseline_epoch=baseline["epoch"], baseline_revision=baseline.get("revision", 1),
                      navigation_authority=False, clock_basis="mac_monotonic_capture_interval_midpoints_or_legacy_receive_times",
                      physical_capture_clock_alignment="unqualified", antenna_to_camera_offset="unmeasured")
        if fitted["valid"]:
            t = matrix(fitted["T_anchor_map"]); scale = fitted["scale_m_per_map_unit"]; rotation = t[:3, :3]/scale
            keys=list(controls); xyz=np.array([controls[k][:3,3] for k in keys])
            first=0; second=int(np.argmax(np.linalg.norm(xyz-xyz[first],axis=1)))
            third=int(np.argmax(np.linalg.norm(np.cross(xyz-xyz[first],xyz[second]-xyz[first]),axis=1)))
            chosen=list(dict.fromkeys([first,second,third]+np.linspace(0,len(keys)-1,min(12,len(keys)),dtype=int).tolist()))[:12]
            fitted["controls"] = {keys[i]:controls[keys[i]].tolist() for i in chosen}
            fitted["anchor_reference"] = {key: value.tolist() for key, value in anchors.items()}
            fitted["calibration_evidence"] = [{"point": row["point"], "anchor": row["anchor"],
                "distance_m": row["distance_m"], "time_s": row["time_s"]} for row in resolved]
            order = None; levels = {}
            for key, xyz in anchors.items():
                level = (self.floor_priors.get(key) or {}).get("level")
                if level is not None: levels.setdefault(level, []).append(xyz)
            if len(levels) >= 2:
                order = np.mean(levels[max(levels)], axis=0)-np.mean(levels[min(levels)], axis=0)
            up = rotation @ np.mean(ups, axis=0) if len(ups) >= 5 else None
            fitted["floor"] = baseline.get("ground_plane") or floor_from_motion(transform(t, camera_positions) if camera_positions else [], rotation @ first_rotation,
                float(self.config.get("camera_height_m", .0635)), up, order)
            if orientations:
                epoch = orientations[-1][0]; rotations_ref = [rotation @ r for e, r in orientations if e == epoch]
                u, _, vt = np.linalg.svd(np.sum(rotations_ref, axis=0)); mean = u @ np.diag([1., 1., np.linalg.det(u @ vt)]) @ vt
                scatter = max(float(Rotation.from_matrix(mean.T @ r).magnitude()) for r in rotations_ref)
                fitted["ahrs"] = {"valid": len(rotations_ref) >= 5 and scatter < math.radians(10), "reference_epoch": epoch,
                                  "R_anchor_ahrs_reference": mean.tolist(), "max_rotation_scatter_deg": math.degrees(scatter),
                                  "qualification": "matched_visual_rotations_and_supplied_IMU_mount"}
        return generation, fitted

    def _finished(self, future):
        try:
            generation, fitted = future.result()
            with self.lock:
                if generation == self.generation and self.baseline and fitted.get("baseline_revision") == self.baseline.get("revision", 1):
                    self.diagnostic = {k:copy.deepcopy(v) for k,v in fitted.items()
                        if k not in ("controls","anchor_reference","calibration_evidence")}
                    self.rejected_fit = not fitted.get("valid", False)
                    if fitted.get("valid"):
                        self.attachments[fitted["map_id"]] = copy.deepcopy(fitted)
                        self._save()
                self.future = None
        except Exception as exc:
            with self.lock:
                self.diagnostic = {"valid": False, "status": "registration_fit_error", "error": type(exc).__name__+":"+str(exc)[:160]}
                self.future = None

    def _save(self):
        from vision_config import atomic_json
        atomic_json(self.path, {"schema": "rover.tof.registration/1", "attachments": self.attachments})

    def status(self, tof, map_id, revision):
        with self.lock:
            b = tof.get("baseline") or {}; result = copy.deepcopy(self.diagnostic)
            accepted = copy.deepcopy(self.attachments.get(map_id)); latest = copy.deepcopy(self.last_pose)
        result.update(navigation_authority=False, fitting=self.future is not None, floor_priors=self.floor_priors)
        if not b.get("valid") or float(tof.get("collector_age_s", math.inf)) > 5:
            return {**result, "valid": False, "status": "baseline_unavailable"}
        if not accepted or accepted.get("frame_id") != b.get("frame_id") or accepted.get("baseline_epoch") != b.get("epoch"):
            return {**result, "valid": False}
        if map_id == (self.binding or (None,))[0] and self.rejected_fit:
            return {**result, "valid": False}
        if accepted.get("baseline_revision") != b.get("revision", 1):
            # Persistent model evidence is bounded and replaced at each fit;
            # it is not a history journal. Revalidate inactive submaps against
            # small same-frame baseline refinements without inventing a re-fit.
            try:
                reference = accepted["anchor_reference"]
                current = {p["bssid"].lower(): point([p["x_m"],p["y_m"],p["z_m"]]) for p in b["positions"]}
                if max(np.linalg.norm(current[key]-point(value)) for key,value in reference.items()) > .2:
                    raise ValueError("baseline_refinement_requires_refit")
                evidence = accepted["calibration_evidence"]
                checked = fit_range_errors(evidence, current, matrix(accepted["T_anchor_map"]))
                if checked > accepted["acceptance_m"]:
                    raise ValueError("baseline_refinement_validation_failed")
                accepted["baseline_reference_revision"] = accepted["baseline_revision"]
                accepted["baseline_revision"] = b.get("revision",1)
                accepted["baseline_revalidation_rms_m"] = checked
            except (KeyError, ValueError, TypeError):
                return {**result, "valid": False, "status": "awaiting_baseline_revision_refit"}
        try:
            controls = accepted["controls"]; old, current = [], []
            for key, value in controls.items():
                found = self.resolve_anchor(map_id, key, revision)
                if found is None: raise ValueError("registration_control_missing")
                old.append(matrix(value)[:3, 3]); current.append(matrix(found["T_map_keyframe"])[:3, 3])
            correction = similarity(current, old); t = matrix(accepted["T_anchor_map"]) @ correction
            error = float(np.sqrt(np.mean(np.sum((transform(matrix(accepted["T_anchor_map"]), old)-transform(t, current))**2, axis=1))))
            if error > .1: raise ValueError("registration_controls_changed_requires_refit")
            correction_rotation = correction[:3, :3]/np.cbrt(np.linalg.det(correction[:3, :3]))
            for key, value in controls.items():
                current_pose = matrix(self.resolve_anchor(map_id, key, revision)["T_map_keyframe"])
                difference = matrix(value)[:3, :3].T @ correction_rotation @ current_pose[:3, :3]
                if Rotation.from_matrix(difference).magnitude() > math.radians(10):
                    raise ValueError("registration_control_rotation_changed_requires_refit")
            accepted.update(T_anchor_map=t.tolist(), map_revision=revision, fitting=self.future is not None,
                            floor_priors=self.floor_priors, control_reprojection_rms_m=error,
                            control_keyframe_ids=list(controls))
            accepted.pop("controls", None)
            accepted.pop("anchor_reference", None); accepted.pop("calibration_evidence", None)
            if latest and map_id == (self.binding or (None,))[0] and 0 <= time.monotonic()-latest["time_s"] <= 3:
                pose = self._resolved(latest, map_id, revision, {})
                position = transform(t, pose[:3, 3])
                scale = np.cbrt(np.linalg.det(t[:3, :3])); orientation = t[:3, :3]/scale @ pose[:3, :3]
                accepted["current_camera"] = {"position_anchor_m": position.tolist(), "R_anchor_camera": orientation.tolist(),
                    "frame_id": latest["frame_id"], "age_s": time.monotonic()-latest["time_s"], "source": "range_registered_visual_tracking"}
            return accepted
        except (ValueError, KeyError, TypeError, np.linalg.LinAlgError) as exc:
            return {**result, "valid": False, "status": str(exc)}

    def refresh_stream_status(self, tof, map_id, revision):
        # Called only by processed-frame work, never independent camera capture.
        value=self.status(tof,map_id,revision)
        b=tof.get("baseline") or {}
        key=(map_id,revision,b.get("frame_id"),b.get("epoch"),b.get("revision"))
        with self.lock:
            self.stream_cache=(key,time.monotonic(),value)

    def stream_status(self, tof, map_id, revision):
        b=tof.get("baseline") or {}
        key=(map_id,revision,b.get("frame_id"),b.get("epoch"),b.get("revision"))
        with self.lock:
            cached=self.stream_cache
            diagnostic=copy.deepcopy(self.diagnostic)
        if not b.get("valid") or float(tof.get("collector_age_s",math.inf))>5:
            return {**diagnostic,"valid":False,"status":"baseline_unavailable"}
        if cached is None or cached[0]!=key:
            return {**diagnostic,"valid":False,"status":"registration_refresh_pending" if self.attachments.get(map_id) else diagnostic.get("status","awaiting_matched_motion"),"floor_priors":self.floor_priors}
        value=copy.deepcopy(cached[2]); camera=value.get("current_camera")
        if camera is not None:
            camera["age_s"]+=max(0,time.monotonic()-cached[1])
            if camera["age_s"]>3:value.pop("current_camera",None)
        return value

    def close(self):
        self.executor.shutdown(wait=True)


def fit_range_errors(evidence, anchors, t):
    groups = {}
    for row in evidence:
        groups.setdefault(row["anchor"], []).append(row)
    errors = []
    for key, rows in groups.items():
        for i, row in enumerate(sorted(rows, key=lambda r:r["time_s"])):
            if i % 5 == 4:
                errors.append(float(np.linalg.norm(transform(t, point(row["point"]))-anchors[key])-row["distance_m"]))
    if len(errors) < 4 or not np.isfinite(errors).all():
        raise ValueError("validation_evidence_missing")
    return float(np.sqrt(np.mean(np.square(errors))))
