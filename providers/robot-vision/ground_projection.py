"""Calibrated ray/plane intersection. Contact and planar-ground assumptions are explicit."""
from __future__ import annotations

import math


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def calibration_errors(calibration, image_size=None, require_mounting=False):
    if not isinstance(calibration, dict):
        return ["calibration_missing"]
    c = calibration
    errors = []
    if c.get("validated") is not True:
        errors.append("intrinsics_not_validated")
    for key in ("width", "height"):
        if not isinstance(c.get(key), int) or isinstance(c.get(key), bool) or c[key] <= 0:
            errors.append(f"invalid_{key}")
    for key in ("fx", "fy", "cx", "cy"):
        if not _number(c.get(key)):
            errors.append(f"missing_or_invalid_{key}")
    if _number(c.get("fx")) and c["fx"] <= 0:
        errors.append("invalid_fx")
    if _number(c.get("fy")) and c["fy"] <= 0:
        errors.append("invalid_fy")
    d = c.get("distortion")
    if not isinstance(d, list) or len(d) not in (0, 4, 5, 8, 12, 14) or not all(_number(v) for v in d):
        errors.append("distortion_missing_or_invalid")
    if c.get("lens_model", "opencv_pinhole") != "opencv_pinhole":
        errors.append("unsupported_lens_model")
    if not isinstance(c.get("provenance"), str) or not c["provenance"].strip():
        errors.append("calibration_provenance_missing")
    if image_size is not None and list(image_size) != [c.get("width"), c.get("height")]:
        errors.append("calibration_resolution_mismatch")
    if require_mounting:
        mounting = c.get("mounting") or {}
        for key in ("height_m", "pitch_down_deg", "roll_deg"):
            if not _number(mounting.get(key)):
                errors.append(f"mounting_{key}_missing_or_invalid")
        if _number(mounting.get("height_m")) and mounting["height_m"] <= 0:
            errors.append("invalid_mounting_height")
        if _number(mounting.get("pitch_down_deg")) and abs(mounting["pitch_down_deg"]) > 90:
            errors.append("invalid_mounting_pitch")
    return errors


def project_ground(pixel, calibration, image_size, *, confirmed_floor_contact=False):
    """Return coordinates relative to the ground below the camera, never robot center."""
    base = {
        "availability": "unavailable",
        "position": None,
        "coordinate_frame": "camera_ground",
        "axes": {"x": "forward", "y": "left", "z": "up"},
        "units": "m",
        "method": "undistorted_camera_ray_ground_plane_intersection",
        "calibration_id": (calibration or {}).get("calibration_id"),
        "robot_center_transform": None,
    }
    if confirmed_floor_contact is not True:
        return {**base, "reason": "floor_contact_not_confirmed"}
    errors = calibration_errors(calibration, image_size, require_mounting=True)
    if errors:
        return {**base, "reason": "calibration_unavailable", "details": errors}
    if not isinstance(pixel, (list, tuple)) or len(pixel) != 2 or not all(_number(v) for v in pixel):
        return {**base, "reason": "invalid_pixel"}
    u, v = pixel
    if not (0 <= u < image_size[0] and 0 <= v < image_size[1]):
        return {**base, "reason": "pixel_outside_image"}

    import cv2
    import numpy as np

    c = calibration
    K = np.array([[c["fx"], 0, c["cx"]], [0, c["fy"], c["cy"]], [0, 0, 1]], dtype=float)
    distortion = np.array(c["distortion"], dtype=float) if c["distortion"] else None
    mounting = c["mounting"]
    pitch = math.radians(mounting["pitch_down_deg"])
    roll = math.radians(mounting["roll_deg"])
    sp, cp = math.sin(pitch), math.cos(pitch)
    sr, cr = math.sin(roll), math.cos(roll)

    def intersect(p):
        xn, yn = cv2.undistortPoints(np.array([[p]], dtype=float), K, distortion)[0, 0]
        right = cr*xn - sr*yn
        down_unpitched = sr*xn + cr*yn
        down = sp + cp*down_unpitched
        forward = cp - sp*down_unpitched
        if down <= 1e-9:
            return None, float(down)
        scale = mounting["height_m"]/down
        return np.array([scale*forward, -scale*right, 0.0]), float(down)

    position, down = intersect(pixel)
    if position is None or not np.all(np.isfinite(position)):
        return {**base, "reason": "ray_at_or_above_ground_horizon", "ray_down_component": down}
    jacobian = []
    for direction in ([0.5, 0], [0, 0.5]):
        p1, _ = intersect([u+direction[0], v+direction[1]])
        p0, _ = intersect([u-direction[0], v-direction[1]])
        jacobian.append(None if p0 is None or p1 is None else (p1-p0).tolist())
    return {
        **base, "availability": "available", "position": position.tolist(),
        "quality": "estimated_from_calibrated_ray_and_supplied_plane",
        "ground_contact": "asserted_by_caller",
        "ground_plane": "locally_flat_at_supplied_camera_height",
        "mounting_provenance": mounting.get("provenance"),
        "mounting_validated": mounting.get("validated") is True,
        "ray_down_component": down,
        "pixel_sensitivity": {"d_position_du_m_per_px": jacobian[0], "d_position_dv_m_per_px": jacobian[1]},
        "covariance": None,
        "covariance_status": "calibration_and_contact_uncertainty_not_supplied",
        "global_map_position": None,
        "global_map_position_status": "independent_monocular_scale_and_robot_extrinsics_not_assumed",
    }
