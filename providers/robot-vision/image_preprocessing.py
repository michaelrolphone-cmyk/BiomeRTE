"""Explicit photometric input policy for SLAM; captured evidence stays untouched."""
from __future__ import annotations

import math
import numpy as np


def preprocessing_policy(value=None):
    value = {"mode": "none", "version": 1} if value is None else value
    if not isinstance(value, dict):
        raise ValueError("SLAM preprocessing must be an object")
    mode, version = value.get("mode", "none"), value.get("version", 1)
    if type(version) is not int or version != 1 or mode not in ("none", "percentile_gray"):
        raise ValueError("Unsupported SLAM preprocessing mode or version")
    allowed = {"mode", "version"}
    if mode == "percentile_gray":
        allowed |= {"low_percentile", "high_percentile"}
    if set(value) - allowed:
        raise ValueError("Unknown SLAM preprocessing fields")
    result = {"mode": mode, "version": version}
    if mode == "percentile_gray":
        low, high = value.get("low_percentile", 1.0), value.get("high_percentile", 99.0)
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or
               not math.isfinite(v) for v in (low, high)) or not 0 <= low < high <= 100:
            raise ValueError("SLAM percentiles must satisfy 0 <= low < high <= 100")
        result.update(low_percentile=float(low), high_percentile=float(high))
    return result


def preprocess_slam_image(image_bgr, policy):
    """Return the SLAM buffer and diagnostics measured before normalization."""
    if policy["mode"] == "none":
        return image_bgr, {"state": "raw", "raw_diagnostics": None}
    import cv2
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    low, high = np.percentile(gray, [policy["low_percentile"], policy["high_percentile"]])
    span = float(high-low)
    raw = {
        "minimum": int(gray.min()), "maximum": int(gray.max()),
        "mean": float(gray.mean()), "low_percentile_value": float(low),
        "high_percentile_value": float(high),
        "laplacian_variance": float(cv2.Laplacian(gray, cv2.CV_64F).var()),
    }
    if span <= 0:
        output, gain, state = gray, 1.0, "flat_range"
    else:
        gain, state = 255.0/span, "normalized"
        output = np.clip((gray.astype(np.float32)-float(low))*gain, 0, 255).astype(np.uint8)
    return cv2.cvtColor(output, cv2.COLOR_GRAY2BGR), {
        "state": state, "gain": gain, "raw_diagnostics": raw,
        "quality_claim": "contrast_only_not_optical_sharpness_or_tracking",
    }
