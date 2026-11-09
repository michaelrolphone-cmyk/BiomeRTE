#!/usr/bin/env python3
"""Qualify real native pySLAM persistence using its pinned KITTI06 sample.

This is an offline engine qualification, separate from the uncalibrated live
camera. It stops training when a usable six-keyframe map exists, or when the
finite sample ends. No fabricated pose, metric scale, robot motion, or
arbitrary process timeout is involved.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys
import time
import traceback

import cv2
import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from slam_backend import SlamBackend, PYSLAM_COMMIT, _sha_file


def write_json(path, data):
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True, allow_nan=False))
    temporary.replace(path)


def run(state_dir, output):
    state = Path(state_dir).expanduser().resolve()
    root = state / "vendor/pyslam"
    output = Path(output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    report_path = output / "qualification.json"
    total_started = time.perf_counter()
    provider_root = Path(sys.modules["slam_backend"].__file__).resolve().parent
    video_path = root / "data/videos/kitti06/video_color.mp4"
    settings_path = root / "settings/KITTI04-12.yaml"
    times_path = root / "data/videos/kitti06/times.txt"
    settings = yaml.safe_load(settings_path.read_text())
    calibration = {name: settings["Camera." + name]
                   for name in ("width", "height", "fx", "fy", "cx", "cy")}
    calibration.update(
        distortion=[settings.get("Camera." + name, 0.0)
                    for name in ("k1", "k2", "p1", "p2", "k3")],
        calibration_id="pyslam-kitti04-12-" + _sha_file(settings_path)[:16],
        provenance="pySLAM " + PYSLAM_COMMIT + " settings/KITTI04-12.yaml",
        validated=True,
    )
    config = {"calibration": calibration, "pyslam_root": str(root),
              "fps": float(settings["Camera.fps"]), "num_features": 2000}
    report = {
        "status": "running", "pyslam_commit": PYSLAM_COMMIT,
        "profile": "native Apple Silicon Python-core ORB2 + g2o + DBoW3",
        "purpose": "Real feature-map serialization, restore, and image-based relocalization",
        "dataset": {
            "source": "https://github.com/luigifreda/pyslam/tree/" + PYSLAM_COMMIT + "/data/videos/kitti06",
            "video": str(video_path), "video_sha256": _sha_file(video_path),
            "settings": str(settings_path), "settings_sha256": _sha_file(settings_path),
            "timestamps_sha256": _sha_file(times_path),
        },
        "calibration": calibration, "units": "arbitrary", "scale_status": "unobserved",
        "live_camera_qualification": False, "started_at_unix_s": time.time(),
        "source_sha256": {name: _sha_file(provider_root / name) for name in (
            "slam_backend.py", "setup_slam.py", "tests/test_slam_backend.py")},
        "qualifier_sha256": _sha_file(Path(__file__)),
        "runtime_versions": {name: importlib.metadata.version(name) for name in (
            "numpy", "torch", "torchvision", "kornia", "numba")},
        "elapsed_seconds": {},
    }
    backend = None
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError("Pinned KITTI sample could not be opened")
    timestamps = [float(line) for line in times_path.read_text().splitlines() if line.strip()]
    report["dataset"]["video_frame_count"] = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    report["dataset"]["video_width"] = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    report["dataset"]["video_height"] = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if (report["dataset"]["video_width"], report["dataset"]["video_height"]) != (
            calibration["width"], calibration["height"]):
        raise RuntimeError("Benchmark calibration does not match the actual sample dimensions")
    map_id = "qualification-kitti06-" + output.name
    report["map_id"] = map_id
    write_json(report_path, report)
    tracked = []
    candidates = {}
    try:
        backend = SlamBackend(config, state)
        phase_started = time.perf_counter()
        report["new_start"] = backend.start(map_id)
        report["elapsed_seconds"]["new_start"] = time.perf_counter() - phase_started
        phase_started = time.perf_counter()
        index = 0
        last_keyframes = -1
        while True:
            ok, image = cap.read()
            if not ok:
                raise RuntimeError("Finite benchmark ended before a usable six-keyframe map was established")
            if index >= len(timestamps):
                raise RuntimeError("Sample has more frames than its authoritative timestamp list")
            result = backend.track(image, "kitti06-build-" + str(index), timestamps[index])
            if result["localization"]["valid"]:
                tracked.append({"frame_index": index,
                                "reference_keyframe_id": result["localization"]["reference_keyframe_id"],
                                "T_map_camera": result["localization"]["T_map_camera"]})
                if (not result["is_keyframe"] and
                    getattr(backend._slam.tracking.kf_last, "img_id", None) != index):
                    # Retain real non-keyframe views indexed by their reference.
                    # A single view per live anchor suffices for the restart test.
                    candidates[result["localization"]["reference_keyframe_id"]] = (index, image.copy())
            geometry = result["geometry"]
            if geometry["keyframe_count"] != last_keyframes:
                print(json.dumps({"phase": "training", "frame_index": index,
                                  "localization": result["localization"]["status"],
                                  "geometry": geometry}), flush=True)
                last_keyframes = geometry["keyframe_count"]
                report["training_progress"] = {
                    "frame_index": index, "tracked_frame_count": len(tracked), "geometry": geometry}
                write_json(report_path, report)
            if geometry["keyframe_count"] >= 6 and geometry["available"] and result["localization"]["valid"]:
                live_ids = {str(kf.id) for kf in backend._slam.map.get_keyframes() if not kf.is_bad()}
                usable = [(i, img, anchor) for anchor, (i, img) in candidates.items() if anchor in live_ids]
                if usable:
                    revisit_index, revisit_image, revisit_reference = max(usable, key=lambda entry: entry[0])
                    break
            index += 1
        positions = np.array([entry["T_map_camera"] for entry in tracked])[:, :3, 3]
        if len(positions) < 2 or float(np.max(np.linalg.norm(positions - positions[0], axis=1))) <= 1e-6:
            raise RuntimeError("No nontrivial native camera trajectory was produced")
        report["elapsed_seconds"]["training"] = time.perf_counter() - phase_started
        report["training"] = {
            "frames_processed": index + 1, "tracked_frame_count": len(tracked),
            "nonzero_translation": True, "last_pose": tracked[-1],
            "geometry": result["geometry"],
        }
        phase_started = time.perf_counter()
        checkpoint = backend.checkpoint(output / "system_state")
        report["elapsed_seconds"]["checkpoint"] = time.perf_counter() - phase_started
        saved_document = json.loads((Path(checkpoint["path"]) / "map.json").read_text())
        saved_keyframes = saved_document["map"]["keyframes"]
        saved_ids = {str(frame["id"]) for frame in saved_keyframes}
        saved_image_ids = {int(frame["img_id"]) for frame in saved_keyframes}
        revisit_choices = [(i, img, anchor) for anchor, (i, img) in candidates.items()
                           if anchor in saved_ids and i not in saved_image_ids]
        if not revisit_choices:
            raise RuntimeError("No observed non-keyframe view survives in the saved map")
        revisit_index, revisit_image, revisit_reference = max(revisit_choices, key=lambda entry: entry[0])
        if revisit_reference not in saved_ids:
            raise RuntimeError("Selected revisit reference was culled before the saved snapshot")
        report["checkpoint"] = {
            key: checkpoint[key] for key in ("availability", "path", "files", "format", "format_version",
                                            "pyslam_commit", "geometry", "exact_saved_geometry")}
        report["checkpoint"].update(
            keyframe_descriptor_records=sum(bool(frame.get("des")) for frame in saved_keyframes),
            keypoint_count=sum(len(frame.get("kps", [])) for frame in saved_keyframes),
            loop_database_bytes=(Path(checkpoint["path"]) / "loop_closing.db").stat().st_size,
            serialized_point_count=len(saved_document["map"]["points"]),
        )
        revisit_path = output / "relocalization_input.png"
        if not cv2.imwrite(str(revisit_path), revisit_image):
            raise RuntimeError("Could not preserve actual relocalization evidence image")
        report["relocalization_input"] = {
            "dataset_frame_index": revisit_index, "was_saved_keyframe": False,
            "prior_reference_keyframe_id": revisit_reference,
            "path": str(revisit_path), "sha256": _sha_file(revisit_path),
        }
        write_json(output / "trajectory.json", tracked)
        backend.close()
        backend = None
        report["original_engine_closed_before_restore"] = True
        write_json(report_path, report)

        backend = SlamBackend(config, state)
        phase_started = time.perf_counter()
        start = backend.start(map_id, checkpoint={"availability": "available", "path": checkpoint["path"]})
        report["elapsed_seconds"]["restore"] = time.perf_counter() - phase_started
        report["restored_start"] = start
        if start["localization"]["valid"] or start["localization"]["T_map_camera"] is not None:
            raise AssertionError("Restore exposed an old pose as a current observation")
        if start["status"] != "relocalizing":
            raise AssertionError("Restore did not explicitly enter relocalization")
        write_json(report_path, report)
        phase_started = time.perf_counter()
        result = backend.track(revisit_image, "kitti06-relocalize-" + str(revisit_index), time.monotonic())
        report["elapsed_seconds"]["relocalization"] = time.perf_counter() - phase_started
        report["relocalized"] = {"localization": result["localization"], "geometry": result["geometry"],
                                 "is_keyframe": result["is_keyframe"],
                                 "observation_anchor": result["observation_anchor"],
                                 "full_snapshot_upserts": sum(c["op"] == "upsert" for c in result["map_changes"])}
        if not result["localization"]["valid"] or result["localization"]["status"] != "tracking":
            raise AssertionError("Fresh decoded non-keyframe benchmark image did not relocalize")
        if result["localization"]["reference_keyframe_id"] not in saved_ids:
            raise AssertionError("Relocalization did not reconnect to an existing saved keyframe")
        if not result["geometry"]["available"] or not result["geometry"]["complete_snapshot"]:
            raise AssertionError("Restored state did not export an authoritative nonempty geometry snapshot")
        if not np.isfinite(np.asarray(result["localization"]["T_map_camera"])).all():
            raise AssertionError("Relocalized pose is nonfinite")
        report["status"] = "passed"
        report["scope"] = (
            "One real monocular KITTI prefix and one freshly submitted non-keyframe revisit. "
            "This verifies this native engine profile's state roundtrip and relocalization; "
            "it does not qualify live ESP32 intrinsics, metric scale, latency, or all environments.")
    except BaseException as exc:
        report["status"] = "failed"
        report["error"] = {"type": type(exc).__name__, "message": str(exc),
                           "traceback": traceback.format_exc()}
        raise
    finally:
        cleanup_error = None
        try:
            if backend is not None:
                backend.close()
        except BaseException as exc:
            report["cleanup_error"] = {"type": type(exc).__name__, "message": str(exc),
                                       "traceback": traceback.format_exc()}
            if report["status"] != "failed":
                report["status"] = "failed"
                cleanup_error = exc
        finally:
            cap.release()
            report["finished_at_unix_s"] = time.time()
            report["elapsed_seconds"]["total"] = time.perf_counter() - total_started
            write_json(report_path, report)
            print(json.dumps({"status": report["status"], "report": str(report_path)}), flush=True)
        if cleanup_error is not None:
            raise cleanup_error
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default=str(Path.home() / ".local/share/biome-robot-vision"))
    parser.add_argument("--output", required=True,
                        help="Fresh directory beneath the separate qualification state tree")
    args = parser.parse_args()
    run(args.state_dir, args.output)


if __name__ == "__main__":
    main()
