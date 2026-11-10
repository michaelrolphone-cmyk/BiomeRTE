"""Derive floor-relative robot poses from separately qualified metric geometry.

Pure composition only: this module does not estimate transforms, fuse AHRS,
change maps, infer traversability or command motion. T_A_B maps B into A.
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

BINDING = ("snapshot_id", "frame_id", "map_id", "map_revision", "calibration_id")


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def identity(value):
    return isinstance(value, str) and bool(value.strip())


def rigid_transform(value):
    if not (isinstance(value, list) and len(value) == 4
            and all(isinstance(row, list) and len(row) == 4 for row in value)
            and all(finite(x) for row in value for x in row)):
        raise ValueError("rigid_transform_requires_finite_4x4")
    if any(abs(value[3][i] - (1 if i == 3 else 0)) > 1e-6 for i in range(4)):
        raise ValueError("rigid_transform_invalid_homogeneous_row")
    r = [row[:3] for row in value[:3]]
    if any(abs(sum(r[k][i]*r[k][j] for k in range(3)) - (1 if i == j else 0)) > 1e-5
           for i in range(3) for j in range(3)):
        raise ValueError("rigid_transform_rotation_not_orthonormal")
    determinant = (r[0][0]*(r[1][1]*r[2][2]-r[1][2]*r[2][1])
                   - r[0][1]*(r[1][0]*r[2][2]-r[1][2]*r[2][0])
                   + r[0][2]*(r[1][0]*r[2][1]-r[1][1]*r[2][0]))
    if abs(determinant - 1) > 1e-5:
        raise ValueError("rigid_transform_rotation_not_proper")
    return copy.deepcopy(value)


def compose(left, right):
    return [[sum(left[i][k]*right[k][j] for k in range(4))
             for j in range(4)] for i in range(4)]


def _attachment(record, kind, from_frame, to_frame):
    if not isinstance(record, dict):
        raise ValueError(kind + "_missing")
    if type(record.get("schema_version")) is not int or record["schema_version"] != 1:
        raise ValueError(kind + "_schema_unsupported")
    if record.get("kind") != kind:
        raise ValueError(kind + "_kind_mismatch")
    if not identity(record.get("attachment_id")) or type(record.get("revision")) is not int or record["revision"] < 1:
        raise ValueError(kind + "_identity_invalid")
    if record.get("units") != "m" or record.get("from_frame") != from_frame or record.get("to_frame") != to_frame:
        raise ValueError(kind + "_frames_or_units_mismatch")
    qualification = record.get("qualification")
    if not isinstance(qualification, dict) or qualification.get("status") != "qualified":
        raise ValueError(kind + "_unqualified")
    evidence = qualification.get("evidence")
    if not (isinstance(evidence, list) and evidence and all(identity(x) for x in evidence)
            and identity(qualification.get("method"))):
        raise ValueError(kind + "_qualification_evidence_missing")
    # This validates the supplied qualification record, not the physical truth
    # of its evidence. The acquisition/qualification workflow owns that judgment.
    return rigid_transform(record.get("transform"))


def _empty_pose(snapshot):
    result = {"availability": "unavailable", "valid": False, "T_floor_robot": None,
              "coordinate_frame": "floor", "robot_axes": "x_forward_y_left_z_up",
              "units": "m", "pose_origin": "qualified_robot_origin",
              "navigation_available": False, "source_exposure_time_qualified": False,
              "covariance": None, "velocity": None, "occupancy": None}
    if isinstance(snapshot, dict):
        result.update({key: copy.deepcopy(snapshot.get(key))
                       for key in (*BINDING, "session_id")})
    return result


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_navigation_configuration_key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("nonfinite_navigation_configuration")


def read_robot_pose(snapshot, path, *, delivery_elapsed_s, anchor_resolver=None):
    """Read one bounded attachment document; failures never replace visual data."""
    result = _empty_pose(snapshot)
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("navigation_configuration_too_large")
        document = json.loads(raw, object_pairs_hook=_unique_object,
                              parse_constant=_reject_constant)
        if (not isinstance(document, dict) or type(document.get("schema_version")) is not int
                or document["schema_version"] != 1):
            raise ValueError("navigation_configuration_schema")
    except FileNotFoundError:
        return {**result, "reason": "navigation_frames_not_configured"}
    except (OSError, ValueError, UnicodeError):
        return {**result, "reason": "navigation_frames_configuration_invalid"}
    floor = document.get("floor_alignment")
    resolution = None
    if (anchor_resolver is not None and isinstance(snapshot, dict) and isinstance(floor, dict)
            and floor.get("map_revision") != snapshot.get("map_revision")):
        try:
            resolution = anchor_resolver(floor, snapshot)
            floor = copy.deepcopy(floor)
            floor["map_revision"] = snapshot["map_revision"]
        except (ValueError, KeyError, TypeError):
            return {**result, "reason": "floor_alignment_anchor_resolution_failed"}
    pose = derive_robot_pose(snapshot, floor, document.get("camera_robot_extrinsics"),
                             delivery_elapsed_s=delivery_elapsed_s)
    if pose["valid"] and resolution is not None:
        pose["floor_alignment"]["resolution"] = copy.deepcopy(resolution)
    return pose


def derive_robot_pose(snapshot, floor_alignment, camera_robot_extrinsics, *,
                      delivery_elapsed_s):
    """Return an unavailable reason or the camera-derived robot pose at a frame.

    delivery_elapsed_s is measured by the same-Mac caller around snapshot
    delivery; no wall-clock synchronization or source exposure timing is assumed.
    Floor alignment is accepted only at its explicitly qualified map revision
    and scale solution. A caller must revalidate after geometry corrections.
    """
    result = _empty_pose(snapshot)
    if not isinstance(snapshot, dict):
        return {**result, "reason": "snapshot_missing"}
    try:
        if (not all(identity(snapshot.get(k)) for k in
                    ("snapshot_id", "frame_id", "map_id", "calibration_id", "session_id"))
                or type(snapshot.get("map_revision")) is not int or snapshot["map_revision"] < 0):
            raise ValueError("snapshot_identity_invalid")
        freshness = snapshot.get("freshness")
        if not isinstance(freshness, dict):
            raise ValueError("snapshot_freshness_missing")
        age, threshold = freshness.get("receive_decode_age_s"), freshness.get("freshness_threshold_s")
        if not (freshness.get("is_fresh") is True and freshness.get("is_current_session") is True
                and finite(age) and age >= 0 and finite(threshold) and threshold > 0
                and finite(delivery_elapsed_s) and delivery_elapsed_s >= 0
                and age + delivery_elapsed_s <= threshold):
            raise ValueError("snapshot_stale_or_unqualified")
        metric = snapshot.get("metric_localization")
        if not isinstance(metric, dict):
            raise ValueError("metric_camera_pose_missing")
        if not (metric.get("availability") == "available" and metric.get("valid") is True
                and metric.get("units") == "m" and metric.get("coordinate_frame") == "map_metric"
                and metric.get("pose_origin") == "camera_optical"):
            raise ValueError("metric_camera_pose_unavailable")
        if (type(metric.get("map_revision")) is not int
                or any(metric.get(k) != snapshot[k] for k in BINDING)
                or not identity(metric.get("attachment_id"))
                or not identity(metric.get("solution_id"))):
            raise ValueError("metric_camera_pose_binding_mismatch")
        t_map_camera = rigid_transform(metric.get("T_map_camera"))
        t_floor_map = _attachment(floor_alignment, "map_floor_alignment", "map_metric", "floor")
        t_camera_robot = _attachment(camera_robot_extrinsics, "camera_robot_extrinsics",
                                     "robot", "camera_optical")
        if (type(floor_alignment.get("map_revision")) is not int
                or any(floor_alignment.get(k) != snapshot[k] for k in
                       ("map_id", "map_revision", "calibration_id"))
                or floor_alignment.get("metric_attachment_id") != metric["attachment_id"]
                or floor_alignment.get("metric_solution_id") != metric["solution_id"]):
            raise ValueError("floor_alignment_binding_mismatch")
        if (camera_robot_extrinsics.get("calibration_id") != snapshot["calibration_id"]
                or not identity(camera_robot_extrinsics.get("physical_mount_id"))
                or not identity(camera_robot_extrinsics.get("robot_model_id"))):
            raise ValueError("camera_robot_extrinsics_binding_mismatch")
        t_floor_robot = rigid_transform(compose(compose(t_floor_map, t_map_camera), t_camera_robot))
        forward_xy = math.hypot(t_floor_robot[0][0], t_floor_robot[1][0])
        heading = (math.atan2(t_floor_robot[1][0], t_floor_robot[0][0])
                   if forward_xy > 1e-6 else None)
        result.update(
            availability="available", valid=True, reason=None,
            T_floor_robot=t_floor_robot,
            heading_rad=heading,
            heading_status="floor_relative_robot_forward_axis" if heading is not None else "robot_forward_axis_vertical",
            translation_m=[t_floor_robot[i][3] for i in range(3)],
            age_at_receipt_upper_estimate_s=age + delivery_elapsed_s,
            metric_attachment_id=metric["attachment_id"], metric_solution_id=metric["solution_id"],
            floor_alignment={"attachment_id": floor_alignment["attachment_id"],
                             "revision": floor_alignment["revision"]},
            camera_robot_extrinsics={"attachment_id": camera_robot_extrinsics["attachment_id"],
                                     "revision": camera_robot_extrinsics["revision"],
                                     "physical_mount_id": camera_robot_extrinsics["physical_mount_id"],
                                     "robot_model_id": camera_robot_extrinsics["robot_model_id"]},
            derivation="T_floor_map_metric * T_map_metric_camera * T_camera_robot",
            uncertainty_status="joint_camera_scale_mounting_floor_uncertainty_not_estimated",
        )
    except (ValueError, OverflowError) as exc:
        result["reason"] = str(exc)
    return result
