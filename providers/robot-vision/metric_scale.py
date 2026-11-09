"""Pure metric-scale geometry for exact image-keyframe observations.

No function performs I/O, moves hardware, changes a map, or applies its result.
T_map_camera and T_board_camera map optical camera coordinates into their named
frames. Map translations use arbitrary units; board translations use metres
(or nominal metres while printed size remains unverified). Revisions must have
been reconciled by the caller against the exact current keyframe poses.

DEFAULT_POLICY values are configurable engineering acceptance gates, not
statistical confidence guarantees or limits on collection/runtime/retention.
The caller must retain the original observations and re-estimate after relevant
keyframe corrections. A board's physical immobility is an explicit assumption;
a monocular fit cannot prove that a coherently moving reference was stationary.
"""
from __future__ import annotations

import math
from itertools import combinations
import numpy as np

DEFAULT_POLICY = {
    "min_training_observations": 4,
    "min_validation_observations": 2,
    "min_baseline_m": 0.15,
    "min_position_separation_m": 0.01,
    "max_board_reprojection_rms_px": 2.0,
    "max_orientation_error_deg": 5.0,
    "max_training_position_rms_m": 0.025,
    "max_position_error_m": 0.05,
    "max_validation_position_error_m": 0.05,
    "min_validation_baseline_m": 0.05,
    "max_validation_scale_relative_error": 0.10,
    "min_inlier_fraction": 0.75,
    "max_pairwise_scale_relative_mad": 0.10,
    "ambiguous_scale_relative_difference": 0.05,
    "ambiguity_cost_margin": 0.02,
}

def validate_policy(policy=None):
    """Return normalized JSON policy; reject unknown or invalid overrides."""
    if policy is not None and not isinstance(policy, dict):
        raise ValueError("policy must be a dict")
    values = dict(DEFAULT_POLICY)
    unknown = set(policy or {}) - set(values)
    if unknown:
        raise ValueError("Unknown metric-scale policy: " + ", ".join(sorted(unknown)))
    values.update(policy or {})
    for key, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float, np.number)):
            raise ValueError(f"{key} must be a finite number")
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise ValueError(f"{key} must be finite and positive")
        if key.startswith("min_") and key.endswith("_observations"):
            minimum = 4 if key == "min_training_observations" else 2
            if number != int(number) or number < minimum:
                raise ValueError(f"{key} must be an integer >= {minimum}")
            values[key] = int(number)
        else:
            values[key] = number
    if not 0 < values["min_inlier_fraction"] <= 1:
        raise ValueError("min_inlier_fraction must be in (0, 1]")
    if values["max_orientation_error_deg"] >= 90:
        raise ValueError("max_orientation_error_deg must be less than 90")
    return values

class _InputError(ValueError):
    def __init__(self, code, detail):
        self.code = code
        super().__init__(detail)

def _number(value, code, *, positive=False):
    if isinstance(value, bool):
        raise _InputError(code, "Boolean is not a measurement")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise _InputError(code, "Expected a finite measurement")
    if not math.isfinite(result) or (positive and result <= 0):
        raise _InputError(code, "Expected a finite positive measurement" if positive else "Expected finite measurement")
    return result

def _identity(value, code):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value):
        raise _InputError(code, "Missing or invalid identity")
    return str(value)

def _pose(value, code):
    try:
        matrix = np.asarray(value, dtype=float)
    except (TypeError, ValueError):
        raise _InputError(code, "Pose must be a finite 4x4 matrix")
    if matrix.shape != (4, 4) or not np.isfinite(matrix).all():
        raise _InputError(code, "Pose must be a finite 4x4 matrix")
    R = matrix[:3, :3]
    if (not np.allclose(matrix[3], [0, 0, 0, 1], atol=1e-8, rtol=0)
            or not np.allclose(R.T @ R, np.eye(3), atol=1e-4, rtol=0)
            or not math.isclose(float(np.linalg.det(R)), 1., abs_tol=1e-4)):
        raise _InputError(code, "Pose rotation must be proper orthonormal, with homogeneous last row")
    return matrix

def _reference(value):
    if not isinstance(value, dict):
        raise _InputError("invalid_reference", "reference must be an object")
    inner = value.get("inner_corners")
    if (not isinstance(inner, (list, tuple)) or len(inner) != 2
            or any(isinstance(x, bool) or not isinstance(x, int) or x < 3 for x in inner)):
        raise _InputError("invalid_reference", "inner_corners must contain two integers >=3")
    if inner[0] == inner[1]:
        raise _InputError("unsupported_square_pattern", "This helper handles rectangular checkerboards and their 180-degree ordering ambiguity")
    square = _number(value.get("square_size_m"), "invalid_reference", positive=True)
    return (int(inner[0]), int(inner[1]), square)

def _object_points(reference):
    columns, rows, square = reference
    points = np.zeros((columns*rows, 3), dtype=float)
    points[:, :2] = np.mgrid[0:columns, 0:rows].T.reshape(-1, 2) * square
    return points

def board_pose_candidates(corners_px, calibration, board_spec):
    """Return plausible metric camera-to-board poses for both 180-degree orders.

    calibration is the service's validated opencv_pinhole dictionary (fx, fy,
    cx, cy, width, height, distortion). board_spec has inner_corners and
    square_size_m. Negative-depth candidates are discarded. Returned branches
    are measurements only; size verification and stationary assumptions are
    enforced by estimate_metric_attachment.
    """
    import cv2 as cv
    reference = _reference(board_spec)
    if not isinstance(calibration, dict) or calibration.get("validated") is not True:
        raise ValueError("A validated camera calibration is required")
    if calibration.get("lens_model", "opencv_pinhole") != "opencv_pinhole":
        raise ValueError("Only the OpenCV pinhole model is supported")
    fx = _number(calibration.get("fx"), "invalid_calibration", positive=True)
    fy = _number(calibration.get("fy"), "invalid_calibration", positive=True)
    cx = _number(calibration.get("cx"), "invalid_calibration")
    cy = _number(calibration.get("cy"), "invalid_calibration")
    width = _number(calibration.get("width"), "invalid_calibration", positive=True)
    height = _number(calibration.get("height"), "invalid_calibration", positive=True)
    K = np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1.]])
    D = np.asarray(calibration.get("distortion", []), dtype=float).reshape(-1)
    if D.size not in (4, 5, 8, 12, 14) or not np.isfinite(D).all():
        raise ValueError("Invalid pinhole distortion coefficients")
    points = np.asarray(corners_px, dtype=float)
    expected = reference[0] * reference[1]
    if points.shape == (expected, 1, 2):
        points = points[:, 0]
    if points.shape != (expected, 2) or not np.isfinite(points).all():
        raise ValueError("Unexpected/nonfinite checkerboard corner array")
    if not ((points >= 0).all() and (points[:, 0] < width).all() and (points[:, 1] < height).all()):
        raise ValueError("Checkerboard corners are outside the calibrated image")
    obj = _object_points(reference)
    candidates = []
    for order, observed in (("detected", points), ("reversed", points[::-1].copy())):
        answer = cv.solvePnPGeneric(obj, observed, K, D, flags=cv.SOLVEPNP_IPPE)
        if not answer[0]:
            continue
        for branch, (rvec, tvec) in enumerate(zip(answer[1], answer[2])):
            R, _ = cv.Rodrigues(rvec)
            t = np.asarray(tvec, dtype=float).reshape(3)
            depth = (obj @ R.T + t)[:, 2]
            projected, _ = cv.projectPoints(obj, rvec, tvec, K, D)
            rms = float(np.sqrt(np.mean(np.sum((observed - projected.reshape(-1, 2))**2, axis=1))))
            if not np.isfinite(R).all() or not np.isfinite(t).all() or not math.isfinite(rms) or np.min(depth) <= 0:
                continue
            pose = np.eye(4)
            pose[:3, :3] = R.T
            pose[:3, 3] = -R.T @ t
            candidates.append({
                "T_board_camera": pose.tolist(), "reprojection_rms_px": rms,
                "min_corner_depth_m": float(np.min(depth)),
                "corner_order": order, "pnp_branch": int(branch),
            })
    return candidates

def _common(observation):
    if not isinstance(observation, dict):
        raise _InputError("invalid_observation", "Observation must be an object")
    result = {"raw": observation}
    for key in ("observation_id", "frame_id", "snapshot_id", "map_id", "calibration_id", "keyframe_id"):
        result[key] = _identity(observation.get(key), "invalid_" + key)
    for key in ("source_map_revision", "resolved_map_revision"):
        value = observation.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _InputError("invalid_map_revision", "Revisions must be nonnegative integers")
        result[key] = value
    if result["source_map_revision"] > result["resolved_map_revision"]:
        raise _InputError("invalid_map_revision", "Source revision is newer than resolved revision")
    matrix = _pose(observation.get("T_map_camera"), "invalid_map_camera_pose")
    result.update(T_map_camera=matrix, C_map=matrix[:3, 3], R_map_camera=matrix[:3, :3])
    return result

def _consistent(observations, *, board=False):
    if not observations:
        return {}
    for key in ("map_id", "calibration_id", "resolved_map_revision"):
        if len({o[key] for o in observations}) != 1:
            raise _InputError(key + "_mismatch", f"All observations must have one {key}")
    for key in ("observation_id", "frame_id", "snapshot_id", "keyframe_id"):
        if len({o[key] for o in observations}) != len(observations):
            raise _InputError("duplicate_" + key, f"Each exact-image keyframe must have distinct {key}")
    meta = {key: observations[0][key] for key in ("map_id", "calibration_id", "resolved_map_revision")}
    if board:
        if len({o["reference_board_id"] for o in observations}) != 1:
            raise _InputError("reference_board_id_mismatch", "Reference board identity changed")
        if len({o["reference_geometry"] for o in observations}) != 1:
            raise _InputError("reference_geometry_mismatch", "Reference dimensions changed")
        meta["reference_board_id"] = observations[0]["reference_board_id"]
    return meta

def _board_observation(raw, policy):
    result = _common(raw)
    result["reference_board_id"] = _identity(raw.get("reference_board_id"), "invalid_reference_board_id")
    reference = raw.get("reference")
    geometry = _reference(reference)
    if reference.get("stationary") is not True:
        raise _InputError("reference_not_stationary", "Stationary reference must be explicitly declared")
    if not isinstance(reference.get("size_verified"), bool):
        raise _InputError("invalid_reference", "size_verified must be a boolean")
    if not isinstance(reference.get("measurement_source"), str) or not reference["measurement_source"].strip():
        raise _InputError("invalid_reference", "A measurement source is required")
    result.update(reference_geometry=geometry, size_verified=reference["size_verified"])
    obj = _object_points(geometry)
    supplied = raw.get("board_pose_candidates")
    if not isinstance(supplied, list):
        raise _InputError("invalid_board_pose_candidates", "board_pose_candidates must be a list")
    candidates = []
    for index, value in enumerate(supplied):
        if not isinstance(value, dict):
            raise _InputError("invalid_board_pose_candidate", "Candidate must be an object")
        pose = _pose(value.get("T_board_camera"), "invalid_board_camera_pose")
        rms = _number(value.get("reprojection_rms_px"), "invalid_board_reprojection_error")
        if rms < 0:
            raise _InputError("invalid_board_reprojection_error", "Reprojection RMS cannot be negative")
        # Recompute cheirality from the actual board geometry, not a supplied flag.
        depth = ((obj - pose[:3, 3]) @ pose[:3, :3])[:, 2]
        if rms > policy["max_board_reprojection_rms_px"] or np.min(depth) <= 0:
            continue
        candidates.append({
            "C_board": pose[:3, 3], "R_board_camera": pose[:3, :3],
            "relative_rotation": pose[:3, :3] @ result["R_map_camera"].T,
            "rms": rms, "input_index": index,
            "corner_order": value.get("corner_order"), "pnp_branch": value.get("pnp_branch"),
        })
    result["candidates"] = candidates
    return result

def _result(meta, status, failed, *, scale=None, rotation=None, translation=None,
            quality=None, training=(), validation=()):
    return {
        **meta, "qualified": status == "qualified", "status": status,
        "failed_checks": list(dict.fromkeys(failed)),
        "scale_m_per_map_unit": None if scale is None else float(scale),
        "R_board_map": None if rotation is None else np.asarray(rotation).tolist(),
        "t_board_map_m": None if translation is None else np.asarray(translation).tolist(),
        "quality": quality or {},
        "training_observation_ids": list(training),
        "validation_observation_ids": list(validation),
    }

def _mean_rotation(rotations):
    U, _, Vt = np.linalg.svd(np.sum(rotations, axis=0))
    correction = np.eye(3)
    correction[2, 2] = np.linalg.det(U @ Vt)
    return U @ correction @ Vt

def _pack(observations):
    n, count = len(observations), max(len(o["candidates"]) for o in observations)
    centers = np.zeros((n, count, 3))
    rotations = np.zeros((n, count, 3, 3))
    rms = np.full((n, count), np.inf)
    valid = np.zeros((n, count), dtype=bool)
    for i, observation in enumerate(observations):
        for j, candidate in enumerate(observation["candidates"]):
            centers[i, j] = candidate["C_board"]
            rotations[i, j] = candidate["relative_rotation"]
            rms[i, j] = candidate["rms"]
            valid[i, j] = True
    return {
        "obs": observations, "map": np.array([o["C_map"] for o in observations]),
        "board": centers, "rotation": rotations, "rms": rms, "valid": valid,
    }

def _assign(model, packed, policy, *, validation=False):
    scale, R, t = model
    predicted = scale * (packed["map"] @ R.T) + t
    position = np.linalg.norm(packed["board"] - predicted[:, None, :], axis=2)
    traces = np.einsum("ncij,ij->nc", packed["rotation"], R)
    angle = np.degrees(np.arccos(np.clip((traces-1.)/2., -1., 1.)))
    tolerance = policy["max_validation_position_error_m"] if validation else policy["max_position_error_m"]
    cost = ((position/tolerance)**2 + (angle/policy["max_orientation_error_deg"])**2
            + .05*(packed["rms"]/policy["max_board_reprojection_rms_px"])**2)
    cost[~packed["valid"]] = np.inf
    choice = np.argmin(cost, axis=1)
    indices = np.arange(len(choice))
    pos, ang, selected_cost = position[indices, choice], angle[indices, choice], cost[indices, choice]
    good = packed["valid"][indices, choice] & (pos <= tolerance) & (ang <= policy["max_orientation_error_deg"])
    return choice, good, pos, ang, selected_cost

def _map_epsilon(points):
    return np.finfo(float).eps * max(1., float(np.max(np.abs(points)))) * 64.

def _refine(model, packed, policy):
    seen, previous = set(), None
    while True:
        choice, good, pos, ang, costs = _assign(model, packed, policy)
        signature = tuple(int(c) if keep else -1 for c, keep in zip(choice, good))
        if signature == previous:
            return model, (choice, good, pos, ang, costs), signature
        if signature in seen or np.count_nonzero(good) < 2:
            return None
        seen.add(signature)
        previous = signature
        indices = np.flatnonzero(good)
        R = _mean_rotation(packed["rotation"][indices, choice[indices]])
        X, Y = packed["map"][indices], packed["board"][indices, choice[indices]]
        xc, yc = X - X.mean(axis=0), Y - Y.mean(axis=0)
        denominator = float(np.sum(xc*xc))
        if denominator <= _map_epsilon(X)**2:
            return None
        scale = float(np.sum((xc @ R.T)*yc) / denominator)
        if not math.isfinite(scale) or scale <= 0:
            return None
        t = Y.mean(axis=0) - scale * (R @ X.mean(axis=0))
        model = (scale, R, t)

def _hypotheses(packed, policy):
    """Deterministic two-pose consensus, without random seeds or sample caps."""
    results, seen_assignments = {}, set()
    epsilon = _map_epsilon(packed["map"])
    for i, j in combinations(range(len(packed["obs"])), 2):
        dx = packed["map"][j] - packed["map"][i]
        denominator = float(dx @ dx)
        if denominator <= epsilon**2:
            continue
        for ci in np.flatnonzero(packed["valid"][i]):
            for cj in np.flatnonzero(packed["valid"][j]):
                R = _mean_rotation([packed["rotation"][i, ci], packed["rotation"][j, cj]])
                dy = packed["board"][j, cj] - packed["board"][i, ci]
                if np.linalg.norm(dy) < policy["min_position_separation_m"]:
                    continue
                scale = float((R @ dx) @ dy / denominator)
                if not math.isfinite(scale) or scale <= 0:
                    continue
                t = ((packed["board"][i, ci] + packed["board"][j, cj])/2
                     - scale * R @ ((packed["map"][i] + packed["map"][j])/2))
                model = (scale, R, t)
                choice, good, _, _, _ = _assign(model, packed, policy)
                signature = tuple(int(c) if keep else -1 for c, keep in zip(choice, good))
                if signature in seen_assignments or np.count_nonzero(good) < policy["min_training_observations"]:
                    continue
                seen_assignments.add(signature)
                fitted = _refine(model, packed, policy)
                if fitted is None:
                    continue
                refined, assignment, signature = fitted
                count = int(np.count_nonzero(assignment[1]))
                if count < policy["min_training_observations"]:
                    continue
                score = float(np.mean(assignment[4][assignment[1]]))
                results[signature] = {"model": refined, "assignment": assignment, "count": count, "score": score}
    return sorted(results.values(), key=lambda v: (-v["count"], v["score"]))

def _baseline(points):
    if len(points) < 2:
        return 0.
    return float(max(np.linalg.norm(points[i]-points[j]) for i, j in combinations(range(len(points)), 2)))

def _distinct_count(points, separation):
    representatives = []
    for point in points:
        if all(np.linalg.norm(point-other) >= separation for other in representatives):
            representatives.append(point)
    return len(representatives)

def _anchor_evidence(observations):
    return [{
        "observation_id": o["observation_id"], "frame_id": o["frame_id"], "snapshot_id": o["snapshot_id"],
        "keyframe_id": o["keyframe_id"], "source_map_revision": o["source_map_revision"],
        "resolved_map_revision": o["resolved_map_revision"], "T_map_camera": o["T_map_camera"].tolist(),
    } for o in observations]

def estimate_metric_attachment(observations, *, validation_sample_ids, policy=None):
    """Fit on training keyframes; validate held-out keyframes without refitting.

    Unknown/invalid policy raises ValueError. Ordinary incomplete observations
    return provisional results; incompatible/corrupt geometry returns rejected.
    A verified physical size is necessary for qualified=True.
    """
    p = validate_policy(policy)
    meta = {}
    try:
        parsed = [_board_observation(o, p) for o in observations]
        meta = _consistent(parsed, board=True)
        requested = [_identity(i, "invalid_validation_id") for i in validation_sample_ids]
        if len(set(requested)) != len(requested):
            raise _InputError("duplicate_validation_id", "Validation identities must be unique")
        identities = {o["observation_id"] for o in parsed}
        if not set(requested) <= identities:
            raise _InputError("unknown_validation_id", "Validation observation not in input")
        training = [o for o in parsed if o["observation_id"] not in requested and o["candidates"]]
        validation = [o for o in parsed if o["observation_id"] in requested]
        failed = []
        if len(training) < p["min_training_observations"]:
            failed.append("insufficient_training_observations")
        if len(validation) < p["min_validation_observations"]:
            failed.append("insufficient_validation_observations")
        quality = {
            "policy": p, "training_observations_available": len(training),
            "validation_observations_available": len(validation),
            "stationary_reference_is_assumed": True,
            "anchors": _anchor_evidence(parsed),
            "pose_candidate_filtered_observation_ids": [o["observation_id"] for o in parsed if not o["candidates"]],
        }
        if len(training) < p["min_training_observations"]:
            return _result(meta, "provisional", failed, quality=quality, validation=requested)
        packed = _pack(training)
        if _baseline(packed["map"]) <= _map_epsilon(packed["map"]):
            return _result(meta, "rejected", ["insufficient_slam_translation"], quality=quality, validation=requested)
        models = _hypotheses(packed, p)
        if not models:
            return _result(meta, "rejected", ["no_consistent_similarity"], quality=quality, validation=requested)
        best = models[0]
        scale, R, t = best["model"]
        choice, good, pos, ang, _ = best["assignment"]
        indices = np.flatnonzero(good)
        X, Y = packed["map"][indices], packed["board"][indices, choice[indices]]
        inlier_ids = [training[i]["observation_id"] for i in indices]
        alternative_scales = [
            m["model"][0] for m in models[1:]
            if m["count"] == best["count"] and m["score"] <= best["score"] + p["ambiguity_cost_margin"]
            and abs(m["model"][0]/scale - 1) > p["ambiguous_scale_relative_difference"]
        ]
        if alternative_scales:
            quality["competing_scales_m_per_map_unit"] = [float(scale)] + [float(s) for s in alternative_scales]
            return _result(meta, "rejected", ["ambiguous_pose_solution"], quality=quality,
                           training=inlier_ids, validation=requested)
        pair_scales = []
        for i, j in combinations(range(len(X)), 2):
            metric_distance = float(np.linalg.norm(Y[i]-Y[j]))
            map_distance = float(np.linalg.norm(X[i]-X[j]))
            if metric_distance >= p["min_validation_baseline_m"] and map_distance > _map_epsilon(X):
                pair_scales.append(metric_distance/map_distance)
        spread = None
        if pair_scales:
            median = float(np.median(pair_scales))
            spread = float(np.median(np.abs(np.asarray(pair_scales)-median))/median)
        quality.update({
            "training_inlier_count": len(indices), "training_inlier_fraction": len(indices)/len(training),
            "training_baseline_m": _baseline(Y), "training_baseline_map_units": _baseline(X),
            "training_distinct_positions": _distinct_count(Y, p["min_position_separation_m"]),
            "training_position_rms_m": float(np.sqrt(np.mean(pos[good]**2))),
            "training_position_max_m": float(np.max(pos[good])),
            "training_orientation_rms_deg": float(np.sqrt(np.mean(ang[good]**2))),
            "training_orientation_max_deg": float(np.max(ang[good])),
            "pairwise_scale_relative_mad": spread,
            "rejected_training_observation_ids": [o["observation_id"] for i, o in enumerate(training) if not good[i]],
            "training_candidate_choices": [{
                "observation_id": training[i]["observation_id"],
                "candidate_index": training[i]["candidates"][choice[i]]["input_index"],
                "corner_order": training[i]["candidates"][choice[i]]["corner_order"],
                "pnp_branch": training[i]["candidates"][choice[i]]["pnp_branch"],
            } for i in indices],
            "reference_size_verified": all(o["size_verified"] for o in parsed),
            "reference_measurement_sources": sorted({o["raw"]["reference"]["measurement_source"] for o in parsed}),
            "board_frame_symmetry": "A constant 180-degree pattern-axis choice is a recorded frame convention, not a scale ambiguity.",
            "uncertainty_note": "Residuals and scale dispersion are empirical checks; physical-reference and calibration uncertainty are not a formal confidence interval.",
        })
        if quality["training_inlier_fraction"] < p["min_inlier_fraction"]:
            failed.append("insufficient_inlier_fraction")
        if quality["training_baseline_m"] + 1e-12 < p["min_baseline_m"]:
            failed.append("insufficient_metric_translation")
        if quality["training_distinct_positions"] < p["min_training_observations"]:
            failed.append("insufficient_distinct_training_positions")
        if quality["training_position_rms_m"] > p["max_training_position_rms_m"]:
            failed.append("training_position_residual")
        if spread is None or spread > p["max_pairwise_scale_relative_mad"]:
            failed.append("inconsistent_pairwise_scale")
        if any(not o["candidates"] for o in validation):
            failed.append("validation_pose_unavailable")
        elif validation:
            vp = _pack(validation)
            vc, vg, vpos, vang, _ = _assign(best["model"], vp, p, validation=True)
            V = vp["board"][np.arange(len(validation)), vc]
            distances = []
            for i in range(len(validation)):
                farthest = int(np.argmax(np.linalg.norm(Y - V[i], axis=1)))
                actual = float(np.linalg.norm(Y[farthest]-V[i]))
                predicted = float(scale*np.linalg.norm(X[farthest]-vp["map"][i]))
                if actual < p["min_validation_baseline_m"]:
                    failed.append("insufficient_validation_baseline")
                else:
                    distances.append(abs(predicted-actual)/actual)
            quality.update({
                "validation_position_rms_m": float(np.sqrt(np.mean(vpos**2))),
                "validation_position_max_m": float(np.max(vpos)),
                "validation_orientation_max_deg": float(np.max(vang)),
                "validation_distinct_positions": _distinct_count(V, p["min_position_separation_m"]),
                "validation_scale_relative_error_max": max(distances) if distances else None,
                "validation_residuals": [{
                    "observation_id": o["observation_id"], "position_error_m": float(vpos[i]),
                    "orientation_error_deg": float(vang[i]),
                    "candidate_index": o["candidates"][vc[i]]["input_index"],
                } for i, o in enumerate(validation)],
            })
            if not np.all(vg):
                failed.append("held_out_pose_residual")
            if len(validation) >= p["min_validation_observations"] and quality["validation_distinct_positions"] < p["min_validation_observations"]:
                failed.append("insufficient_distinct_validation_positions")
            if distances and max(distances) > p["max_validation_scale_relative_error"]:
                failed.append("held_out_scale_error")
        if not quality["reference_size_verified"]:
            failed.append("reference_size_unverified")
        provisional = {"insufficient_validation_observations", "reference_size_unverified"}
        status = "qualified" if not failed else ("provisional" if set(failed) <= provisional else "rejected")
        return _result(meta, status, failed, scale=scale, rotation=R, translation=t,
                       quality=quality, training=inlier_ids, validation=requested)
    except _InputError as error:
        return _result(meta, "rejected", [error.code], quality={"detail": str(error), "policy": p})
    except (TypeError, KeyError) as error:
        return _result(meta, "rejected", ["invalid_observations"], quality={"detail": str(error), "policy": p})

def estimate_displacement_scale(start_observation, end_observation, *, measured_distance_m,
                                validation_measurements=None, policy=None):
    """Scale only from measured camera-center endpoint displacement.

    Validation measurements need entirely separate exact-keyframe endpoints.
    R_board_map and t_board_map_m stay null: no board/floor alignment is claimed.
    measured_distance_m is endpoint chord displacement, not curved path length.
    """
    p = validate_policy(policy)
    meta = {}
    try:
        start, end = _common(start_observation), _common(end_observation)
        validations, all_observations = [], [start, end]
        for item in validation_measurements or []:
            a, b = _common(item["start_observation"]), _common(item["end_observation"])
            distance = _number(item["measured_distance_m"], "invalid_measured_distance", positive=True)
            validations.append((a, b, distance))
            all_observations.extend((a, b))
        # Provide a specific actionable reason for reused validation endpoints.
        primary = {start["keyframe_id"], end["keyframe_id"]}
        seen = set(primary)
        for a, b, _ in validations:
            if a["keyframe_id"] in seen or b["keyframe_id"] in seen or a["keyframe_id"] == b["keyframe_id"]:
                raise _InputError("validation_endpoints_not_independent", "Validation must use distinct keyframe endpoints")
            seen.update((a["keyframe_id"], b["keyframe_id"]))
        meta = _consistent(all_observations)
        distance = _number(measured_distance_m, "invalid_measured_distance", positive=True)
        map_distance = float(np.linalg.norm(end["C_map"]-start["C_map"]))
        quality = {"policy": p, "method": "measured_camera_displacement", "measured_distance_m": distance,
                   "map_distance_units": map_distance, "anchors": _anchor_evidence(all_observations),
                   "measurement_assumption": "Camera-center endpoint chord displacement; not wheel travel or curved path length."}
        if map_distance <= _map_epsilon(np.array([start["C_map"], end["C_map"]])):
            return _result(meta, "rejected", ["insufficient_slam_translation"], quality=quality)
        scale = distance/map_distance
        if not math.isfinite(scale) or scale <= 0:
            return _result(meta, "rejected", ["invalid_scale"], quality=quality)
        failed = []
        if distance < p["min_baseline_m"]:
            failed.append("insufficient_metric_translation")
        if not validations:
            failed.append("insufficient_validation_measurements")
        measured = []
        for a, b, actual in validations:
            delta = float(np.linalg.norm(b["C_map"]-a["C_map"]))
            predicted = scale*delta
            relative = abs(predicted-actual)/actual
            measured.append({"start_observation_id": a["observation_id"], "end_observation_id": b["observation_id"],
                             "measured_distance_m": actual, "predicted_distance_m": predicted,
                             "relative_error": relative})
            if actual < p["min_validation_baseline_m"] or delta <= _map_epsilon(np.array([a["C_map"], b["C_map"]])):
                failed.append("insufficient_validation_baseline")
            if relative > p["max_validation_scale_relative_error"]:
                failed.append("held_out_scale_error")
        quality["validation_measurements"] = measured
        status = "qualified" if not failed else ("provisional" if set(failed) == {"insufficient_validation_measurements"} else "rejected")
        return _result(meta, status, failed, scale=scale, quality=quality,
                       training=[start["observation_id"], end["observation_id"]],
                       validation=[o["observation_id"] for a, b, _ in validations for o in (a, b)])
    except _InputError as error:
        return _result(meta, "rejected", [error.code], quality={"detail": str(error), "policy": p})
    except (TypeError, KeyError) as error:
        return _result(meta, "rejected", ["invalid_observations"], quality={"detail": str(error), "policy": p})
