"""YOLO detections and session-scoped BoT-SORT tracks; no invented 3-D positions."""
from __future__ import annotations

import hashlib
import time
import uuid
from pathlib import Path


class ObjectDetector:
    def __init__(self, config: dict, state_dir):
        self.config = dict(config)
        self.state_dir = Path(state_dir)
        self.model = None
        self.device = None
        self.tracker_epoch = uuid.uuid4().hex
        self._previous = {}
        self._error = None
        self._model_hash = None
        self._last_ms = None
        self._frames = 0

    def start(self):
        import torch
        from ultralytics import YOLO

        model_dir = self.state_dir / "models"
        model_dir.mkdir(parents=True, exist_ok=True)
        name = self.config.get("model", "yolo11n.pt")
        model_path = Path(name) if Path(name).is_absolute() else model_dir / name
        self.device = self.config.get("device", "auto")
        if self.device == "auto":
            self.device = "mps" if torch.backends.mps.is_available() else "cpu"
        self.model = YOLO(str(model_path))
        if model_path.is_file():
            self._model_hash = hashlib.sha256(model_path.read_bytes()).hexdigest()
        self._error = None
        return self

    def reset_tracking(self):
        self.tracker_epoch = uuid.uuid4().hex
        self._previous.clear()
        predictor = getattr(self.model, "predictor", None)
        for tracker in getattr(predictor, "trackers", []) or []:
            tracker.reset()

    def detect(self, image, frame_id: str, timestamp_s: float):
        started = time.monotonic()
        if self.model is None:
            return self.unavailable(frame_id, self._error or "detector_not_started")
        try:
            result = self.model.track(
                image, persist=True, tracker="botsort.yaml",
                imgsz=int(self.config.get("image_size", 640)),
                conf=float(self.config.get("confidence", 0.25)),
                device=self.device, verbose=False,
            )[0]
            items = []
            current = {}
            boxes = result.boxes
            # Transfer each output array once; scalar GPU reads synchronize MPS.
            bounds_rows = boxes.xyxy.detach().cpu().tolist() if boxes is not None else []
            classes = boxes.cls.detach().cpu().tolist() if boxes is not None else []
            confidences = boxes.conf.detach().cpu().tolist() if boxes is not None else []
            ids = boxes.id.detach().cpu().tolist() if boxes is not None and boxes.id is not None else None
            for index, bounds in enumerate(bounds_rows):
                class_id = int(classes[index])
                local_id = int(ids[index]) if ids is not None else None
                track_id = f"{self.tracker_epoch}:{local_id}" if local_id is not None else None
                center = ((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2)
                velocity = None
                if track_id in self._previous:
                    old_center, old_time = self._previous[track_id]
                    dt = timestamp_s - old_time
                    if dt > 0:
                        velocity = [(center[0]-old_center[0])/dt, (center[1]-old_center[1])/dt]
                if track_id is not None:
                    current[track_id] = (center, timestamp_s)
                items.append({
                    "detection_id": f"{frame_id}:d{index}",
                    "track_id": track_id,
                    "tracker_local_id": local_id,
                    "persistent_object_id": None,
                    "class_id": class_id,
                    "label": str(result.names[class_id]),
                    "confidence": float(confidences[index]),
                    "bbox_xyxy_px": bounds,
                    "coordinate_frame": "image_pixels",
                    "image_motion": {
                        "velocity_px_per_s": velocity,
                        "method": "observed_box_center_difference",
                        "includes_camera_motion": True,
                    },
                    "ground_contact": {
                        "status": "unverified",
                        "candidate_pixel": [(bounds[0]+bounds[2])/2, bounds[3]],
                        "reason": "box_bottom_is_not_confirmed_floor_contact",
                    },
                    "position": None,
                    "position_status": "unavailable_unverified_contact_and_depth",
                    "world_velocity": None,
                    "motion_class": "unknown",
                })
            self._previous = current
            self._frames += 1
            self._last_ms = (time.monotonic()-started)*1000
            self._error = None
            return {
                "availability": "available", "frame_id": frame_id,
                "items": items, "tracker_epoch": self.tracker_epoch,
                "tracker": "BoT-SORT", "reidentification": False,
                "inference_ms": self._last_ms,
                "model": self.config.get("model", "yolo11n.pt"),
                "model_sha256": self._model_hash,
                "device": self.device,
                "empty_result_means": "no_supported_object_detected_not_free_space",
            }
        except Exception as exc:
            self._error = f"detector_failed:{type(exc).__name__}"
            self.reset_tracking()
            return self.unavailable(frame_id, self._error)

    def unavailable(self, frame_id, reason):
        return {"availability": "unavailable", "frame_id": frame_id, "reason": reason,
                "items": [], "tracker_epoch": self.tracker_epoch}

    def status(self):
        return {"state": "ready" if self.model is not None and self._error is None else "unavailable",
                "error": self._error, "model": self.config.get("model", "yolo11n.pt"),
                "model_sha256": self._model_hash, "device": self.device,
                "tracker": "BoT-SORT", "tracker_epoch": self.tracker_epoch,
                "processed_frames": self._frames, "last_inference_ms": self._last_ms}
