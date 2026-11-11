#!/usr/bin/env python3
"""Compute intrinsics from varied checkerboard images at the camera's actual stream size."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import uuid

from ground_projection import calibration_errors
from vision_config import atomic_json


def calibrate(images, columns, rows, square_size_m, *, height_m=.0635, pitch_down_deg=0,
              min_views=8):
    import cv2
    import numpy as np

    if columns < 3 or rows < 3 or square_size_m <= 0 or min_views < 3:
        raise ValueError("Use at least a 3x3 inner-corner pattern, positive square size and 3 views")
    pattern = (columns, rows)
    object_template = np.zeros((columns*rows, 3), np.float32)
    object_template[:, :2] = np.mgrid[0:columns, 0:rows].T.reshape(-1, 2)*square_size_m
    objects, points, accepted, rejected = [], [], [], []
    size = None
    for path in images:
        path = Path(path)
        image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            rejected.append({"file": path.name, "reason": "decode_failed"})
            continue
        current_size = (image.shape[1], image.shape[0])
        if size is not None and current_size != size:
            raise ValueError("All calibration images must use the same full stream resolution")
        found, corners = cv2.findChessboardCornersSB(image, pattern,
                                                   cv2.CALIB_CB_EXHAUSTIVE | cv2.CALIB_CB_ACCURACY)
        if not found:
            rejected.append({"file": path.name, "reason": "pattern_not_found"})
            continue
        size = current_size
        objects.append(object_template.copy())
        points.append(corners.astype(np.float32))
        accepted.append({"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    if len(accepted) < min_views:
        raise ValueError(f"Found {len(accepted)} usable views; need {min_views} varied target views")
    rms, K, distortion, rotations, translations = cv2.calibrateCamera(objects, points, size, None, None)
    per_view_rms = []
    for obj, observed, rotation, translation in zip(objects, points, rotations, translations):
        projected, _ = cv2.projectPoints(obj, rotation, translation, K, distortion)
        per_view_rms.append(float(np.sqrt(np.mean(np.sum((observed-projected)**2, axis=2)))))
    value = {
        "calibration_id": "camera-"+uuid.uuid4().hex, "width": size[0], "height": size[1],
        "fx": float(K[0, 0]), "fy": float(K[1, 1]), "cx": float(K[0, 2]), "cy": float(K[1, 2]),
        "distortion": distortion.flatten().tolist(), "lens_model": "opencv_pinhole",
        "validated": True, "provenance": "opencv_multi_view_checkerboard_fit",
        "quality": {"reprojection_rms_px": float(rms), "per_view_rms_px": per_view_rms,
                    "accepted": accepted, "rejected": rejected,
                    "board_inner_corners": list(pattern), "square_size_m": square_size_m,
                    "qualification": "intrinsic_fit_not_navigation_validation"},
        "mounting": {"height_m": height_m, "pitch_down_deg": pitch_down_deg, "roll_deg": 0.0,
                     "provenance": "supplied_mounting_measurements", "validated": False,
                     "height_datum": "optical_center", "roll_status": "assumed_level"},
        "camera_to_robot": None,
    }
    errors = calibration_errors(value, size)
    if errors:
        raise ValueError("Calibration fit invalid: "+", ".join(errors))
    return value


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--images", type=Path, required=True)
    p.add_argument("--columns", type=int, required=True, help="Inner corners, not squares")
    p.add_argument("--rows", type=int, required=True, help="Inner corners, not squares")
    p.add_argument("--square-size-m", type=float, required=True)
    p.add_argument("--height-m", type=float, default=.0635)
    p.add_argument("--pitch-down-deg", type=float, default=0)
    p.add_argument("--min-views", type=int, default=8)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    paths = sorted(path for path in args.images.iterdir()
                   if path.suffix.lower() in (".jpg", ".jpeg", ".png"))
    result = calibrate(paths, args.columns, args.rows, args.square_size_m,
                       height_m=args.height_m, pitch_down_deg=args.pitch_down_deg,
                       min_views=args.min_views)
    atomic_json(args.output, result)
    print(json.dumps({"output": str(args.output), "calibration_id": result["calibration_id"],
                      "reprojection_rms_px": result["quality"]["reprojection_rms_px"],
                      "views": len(result["quality"]["accepted"])}))


if __name__ == "__main__":
    main()
