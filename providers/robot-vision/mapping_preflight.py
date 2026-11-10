#!/usr/bin/env python3
"""Read-only calibration/mapping preflight for the same-Mac vision Unix API.

This report never authorizes driving. It does not start a service, change
configuration, collect calibration, operate the light, or command a relay.
"""
from __future__ import annotations

import argparse
import http.client
import json
import math
from pathlib import Path
import socket
import time

READS = ("robot.vision.metadata", "robot.vision.status", "robot.vision.snapshot")
MAX_RESPONSE = 4 * 1024 * 1024


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def rigid_pose(value):
    if not (isinstance(value, list) and len(value) == 4
            and all(isinstance(row, list) and len(row) == 4 for row in value)
            and all(number(x) for row in value for x in row)):
        return False
    if any(abs(value[3][i] - (1 if i == 3 else 0)) > 1e-6 for i in range(4)):
        return False
    r = [row[:3] for row in value[:3]]
    if any(abs(sum(r[k][i] * r[k][j] for k in range(3))
               - (1 if i == j else 0)) > 1e-3 for i in range(3) for j in range(3)):
        return False
    determinant = (r[0][0] * (r[1][1]*r[2][2]-r[1][2]*r[2][1])
                   - r[0][1] * (r[1][0]*r[2][2]-r[1][2]*r[2][0])
                   + r[0][2] * (r[1][0]*r[2][1]-r[1][1]*r[2][0]))
    return abs(determinant - 1) <= 1e-3


def assess(config, metadata=None, status=None, response=None, *,
           delivery_s=0.0, grid_relocated=False):
    """Summarize actual v1 responses; absent evidence remains an unmet check."""
    config, metadata, status, response = (x if isinstance(x, dict) else {}
                                          for x in (config, metadata, status, response))
    snapshot = response.get("snapshot") or {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    checks = []
    def check(name, passed, action):
        checks.append({"check": name, "passed": passed is True, "next": action})

    check("live_response", status.get("availability") == "available"
          and status.get("state") == "running" and not status.get("error")
          and response.get("availability") == "available",
          "Read a running vision status and a current published snapshot.")
    identities = ("map_id", "session_id")
    coherent = all(isinstance(snapshot.get(k), str) and snapshot[k]
                   and snapshot[k] == status.get(k) for k in identities)
    calibration = metadata.get("calibration") or {}
    calibration = calibration if isinstance(calibration, dict) else {}
    coherent = bool(coherent and snapshot.get("calibration_id")
                    and snapshot["calibration_id"] == calibration.get("calibration_id"))
    check("observation_identity", coherent,
          "Repeat reads after map/session/calibration transitions; preserve snapshot identity.")
    freshness = snapshot.get("freshness") or {}
    freshness = freshness if isinstance(freshness, dict) else {}
    age, threshold = freshness.get("receive_decode_age_s"), freshness.get("freshness_threshold_s")
    fresh = (freshness.get("is_fresh") is True
             and freshness.get("is_current_session") is True
             and number(age) and age >= 0 and number(threshold) and threshold > 0
             and number(delivery_s) and delivery_s >= 0 and age + delivery_s <= threshold)
    check("fresh_decoded_observation", fresh,
          "Obtain a new snapshot; include response-delivery time in its age.")
    calibration_state = status.get("calibration") or {}
    calibration_state = calibration_state if isinstance(calibration_state, dict) else {}
    check("camera_intrinsics", calibration.get("validated") is True
          and calibration_state.get("errors") == []
          and calibration_state.get("calibration_id") == snapshot.get("calibration_id")
          and bool(snapshot.get("calibration_id")),
          "Load the retained validated intrinsics for this exact camera configuration.")
    localization = snapshot.get("localization") or {}
    localization = localization if isinstance(localization, dict) else {}
    check("visual_pose", localization.get("valid") is True
          and rigid_pose(localization.get("T_map_camera")),
          "Initialize or relocalize SLAM; sensor availability does not establish a visual pose.")

    frame = snapshot.get("frame") or {}
    frame = frame if isinstance(frame, dict) else {}
    lighting = frame.get("lighting") or {}
    lighting = lighting if isinstance(lighting, dict) else {}
    luma = lighting.get("mean_luma_0_255")
    light_known = number(luma) and 0 <= luma <= 255
    check("image_brightness", light_known and lighting.get("low_light_observed") is False,
          "Inspect the exact image; use explicit LED control if dark, then inspect a new frame.")

    metric = status.get("metric_calibration") or {}
    metric = metric if isinstance(metric, dict) else {}
    session = metric.get("session") or {}
    session = session if isinstance(session, dict) else {}
    active = session.get("state") in ("collecting", "ready", "provisional")
    scale_actions = []
    if grid_relocated:
        scale_actions.append("Use a new checkerboard placement/session; never mix old and moved-board poses.")
        if active:
            scale_actions.append("Cancel the prior collection before starting the replacement session.")
    scale_actions += [
        "Record measured physical square spacing and its provenance; nominal 20 mm is unverified.",
        "Collect separated exact native keyframes plus held-out validation with the board fixed.",
        "Fit, inspect failed checks, and apply only a qualified scale attachment.",
    ]
    metric_pose = snapshot.get("metric_localization") or {}
    metric_pose = metric_pose if isinstance(metric_pose, dict) else {}
    metric_binding = all(metric_pose.get(k) == snapshot.get(k) and snapshot.get(k) is not None
                         for k in ("snapshot_id", "frame_id", "map_id", "map_revision", "calibration_id"))
    metric_available = (metric_pose.get("availability") == "available"
                        and metric_pose.get("valid") is True and metric_pose.get("units") == "m"
                        and metric_binding and rigid_pose(metric_pose.get("T_map_camera")))

    def enabled(section):
        runtime = status.get(section)
        configured = config.get(section)
        return {"configured": configured.get("enabled") is True if isinstance(configured, dict) else False,
                "running": runtime.get("enabled") is True if isinstance(runtime, dict) else False}
    return {
        "schema_version": 1,
        "scope": "read_only_preparation_not_motion_authorization",
        "mapping_observation_ready": all(x["passed"] for x in checks),
        "checks": checks,
        "identity": {k: snapshot.get(k) for k in (
            "snapshot_id", "frame_id", "map_id", "map_revision", "session_id", "calibration_id")},
        "age_at_receipt_upper_estimate_s": age + delivery_s if number(age) and number(delivery_s) else None,
        "source_exposure_time_qualified": False,
        "optional_ingestion": {name: enabled(name) for name in ("radio", "ahrs")},
        "metric_camera_pose_available": metric_available and coherent and fresh,
        "grid_relocated": grid_relocated,
        "metric_session_id": session.get("session_id"),
        "scale_actions": scale_actions,
        "navigation_available": False,
        "remaining_navigation_software": [
            "Qualified map-to-floor and camera-to-robot transforms with revisioned evidence.",
            "Observed obstacle/traversability layer with unknown space retained, footprint and clearance.",
            "Rear-steering motion model, measured response/stop latency and bounded feedback controller.",
            "Pose-loss/stale-observation stop and waypoint/relocalization qualification.",
        ],
    }


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout=5):
        super().__init__("localhost", timeout=timeout)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        try:
            self.sock.connect(self.path)
        except BaseException:
            self.sock.close()
            raise


def query(path, operation):
    if operation not in READS:
        raise ValueError("preflight_read_operation_required")
    connection = UnixHTTP(path)
    try:
        body = json.dumps({"operation": operation, "inputs": {}})
        connection.request("POST", "/v1/query", body, {"Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise ValueError("preflight_response_too_large")
        if response.status != 200:
            raise ValueError("preflight_query_failed")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("preflight_response_object_required")
        return result
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path,
                        default=Path.home() / ".local/share/biome-robot-vision")
    parser.add_argument("--offline", action="store_true", help="Inspect configuration only.")
    parser.add_argument("--grid-relocated", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = json.loads((args.state_dir / "config.json").read_text())
        if not isinstance(config, dict):
            raise ValueError("configuration_object_required")
        metadata = status = snapshot = None
        delivery_s = 0.0
        if not args.offline:
            path = args.state_dir / "vision.sock"
            metadata = query(path, READS[0])
            status = query(path, READS[1])
            started = time.monotonic()
            snapshot = query(path, READS[2])
            delivery_s = time.monotonic() - started
        report = assess(config, metadata, status, snapshot, delivery_s=delivery_s,
                        grid_relocated=args.grid_relocated)
        print(json.dumps(report, sort_keys=True, indent=2, allow_nan=False))
        return 0 if report["mapping_observation_ready"] else 2
    except (OSError, ValueError, http.client.HTTPException):
        # Never echo raw configuration, URLs, credentials or a server error body.
        print(json.dumps({"availability": "unavailable", "reason": "preflight_read_failed",
                          "navigation_available": False, "motion_commands": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
