"""Continuous RTSP capture with a latest-frame buffer and explicit clock provenance."""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class CapturedFrame:
    image: Any
    metadata: dict
    decoded_monotonic: float
    sequence: int


class CameraSource:
    """Decode in one thread. Slow consumers skip old frames instead of accumulating lag."""

    def __init__(self, config: dict):
        self.config = dict(config)
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._thread = None
        self._latest = None
        self._sequence = 0
        self._started = time.monotonic()
        self._last_decode = None
        self._received = 0
        self._connections = 0
        self._connected = False
        self._error = None
        self._error_details = None
        self._epoch = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="vision-camera", daemon=True)
            self._thread.start()
        return self

    def _run(self):
        try:
            import av
        except ImportError:
            with self._condition:
                self._error = "camera_decoder_dependency_missing"
                self._condition.notify_all()
            return
        while not self._stop.is_set():
            try:
                options = {"rtsp_transport": "tcp"} if self.config["url"].startswith("rtsp:") else {}
                timeout = float(self.config.get("network_timeout_s", 5.0))
                with av.open(self.config["url"], options=options, timeout=(timeout, timeout)) as container:
                    epoch = uuid.uuid4().hex
                    with self._condition:
                        self._epoch = epoch
                        self._connections += 1
                        self._connected = True
                        self._error = None
                        self._error_details = None
                    stream = container.streams.video[0]
                    for frame in container.decode(stream):
                        if self._stop.is_set():
                            break
                        image = frame.to_ndarray(format="bgr24")
                        tick = time.monotonic()
                        timestamp = utc_now()
                        with self._condition:
                            self._sequence += 1
                            self._received += 1
                            self._last_decode = tick
                            metadata = {
                                "frame_id": f"{epoch}:{self._sequence}",
                                "sequence": self._sequence,
                                "stream_epoch": epoch,
                                "width": int(image.shape[1]),
                                "height": int(image.shape[0]),
                                "pixel_format": "bgr24",
                                "capture_time": None,
                                "capture_time_status": "unavailable_no_synchronized_source_clock",
                                "receive_decode_time": timestamp,
                                "receive_decode_clock": "mac_utc_after_decode",
                                "source_media_pts": frame.pts,
                                "source_time_base": str(frame.time_base) if frame.time_base is not None else None,
                                "source_media_seconds": float(frame.pts * frame.time_base)
                                    if frame.pts is not None and frame.time_base is not None else None,
                                "source_media_clock_status": "relative_not_wall_clock",
                            }
                            self._latest = CapturedFrame(image, metadata, tick, self._sequence)
                            self._condition.notify_all()
                    if not self._stop.is_set():
                        raise ConnectionError("camera_stream_ended")
            except Exception as exc:
                with self._condition:
                    self._connected = False
                    # Do not log URLs, credentials, or opaque decoder dumps.
                    self._error = f"camera_decode_failed:{type(exc).__name__}"
                    self._error_details = {
                        "errno": getattr(exc, "errno", None),
                        "reason": getattr(exc, "strerror", None),
                    }
                    self._condition.notify_all()
                self._stop.wait(float(self.config.get("reconnect_delay_s", 1.0)))
        with self._condition:
            self._connected = False
            self._condition.notify_all()

    def next_frame(self, after_sequence: int, timeout: float = 0.5):
        with self._condition:
            self._condition.wait_for(
                lambda: self._stop.is_set()
                or (self._latest is not None and self._latest.sequence > after_sequence),
                timeout=timeout,
            )
            if self._latest is not None and self._latest.sequence > after_sequence:
                return self._latest
            return None

    def status(self):
        with self._condition:
            now = time.monotonic()
            return {
                "state": "connected" if self._connected else "disconnected",
                "error": self._error,
                "error_details": self._error_details,
                "stream_epoch": self._epoch,
                "frames_decoded": self._received,
                "connections": self._connections,
                "reconnects": max(0, self._connections - 1),
                "decode_fps_since_start": self._received / max(now - self._started, 1e-6),
                "last_decode_age_s": None if self._last_decode is None else now - self._last_decode,
                "buffer": "latest_frame_only",
                "capture_time_available": False,
                "configured_source": self.config.get("source_id", "esp32-camera"),
            }

    def close(self):
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        if self._thread:
            self._thread.join(float(self.config.get("network_timeout_s", 5.0)) + 1.0)
