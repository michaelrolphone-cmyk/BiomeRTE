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
    jpeg_bytes: bytes | None = None


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
        self._capture_clock_known = False

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="vision-camera", daemon=True)
            self._thread.start()
        return self

    def _run(self):
        if self.config.get('synchronized_capture') is True:
            return self._run_synchronized()
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

    def _run_synchronized(self):
        import cv2
        import numpy as np
        from urllib.request import Request
        from camera_telemetry import CameraHTTPClient
        from capture_packet import read_capture,CaptureSequence
        client=CameraHTTPClient(self.config,{"network_timeout_s":self.config.get("network_timeout_s",5.0)})
        sequence=CaptureSequence()
        interval=1.0/float(self.config.get('expected_fps',10.0))
        while not self._stop.is_set():
            started=time.monotonic()
            try:
                origin,headers=client._connection()
                headers['Accept']='multipart/mixed'
                with client._open(Request(origin+'/api/capture',headers=headers),timeout=client.network_timeout_s) as response:
                    if response.getcode()!=200:raise ConnectionError('capture_endpoint_unavailable')
                    packet,jpeg=read_capture(response)
                image=cv2.imdecode(np.frombuffer(jpeg,dtype=np.uint8),cv2.IMREAD_COLOR)
                if image is None or image.shape[:2]!=(packet['height'],packet['width']):
                    raise ValueError('capture_dimensions_mismatch')
                tick=time.monotonic()
                source_age=(packet['served_monotonic_us']-packet['capture_monotonic_us'])/1e6
                capture_interval=[started-source_age,tick-source_age]
                tracking_time=sequence.accept(packet,anchor_s=sum(capture_interval)/2.)
                if tracking_time is None:
                    self._stop.wait(interval);continue
                epoch=packet['receiver_id']+':'+packet['boot_id']
                with self._condition:
                    if self._epoch!=epoch or not self._connected:self._connections+=1
                    self._epoch=epoch;self._connected=True;self._error=None;self._error_details=None
                    self._capture_clock_known=True;self._sequence+=1;self._received+=1;self._last_decode=tick
                    metadata=dict(frame_id=packet['frame_id'],sequence=self._sequence,stream_epoch=epoch,source_id=self.config.get('source_id','esp32-camera'),
                        width=packet['width'],height=packet['height'],pixel_format='bgr24',
                        capture_time=None,capture_time_status='device_monotonic_capture_known_utc_unmapped',
                        capture_monotonic_us=packet['capture_monotonic_us'],capture_boot_id=packet['boot_id'],
                        capture_clock='esp_timer',capture_timestamp_semantics=packet.get('capture_timestamp_semantics'),
                        capture_host_monotonic_interval_s=capture_interval,
                        capture_age_at_decode_upper_s=source_age+tick-started,
                        receive_decode_time=utc_now(),receive_decode_clock='mac_utc_after_decode',
                        measurement_monotonic_s=tracking_time,
                        measurement_clock='device_capture_intervals_host_epoch_anchor',
                        source_media_seconds=packet['capture_monotonic_us']/1e6,sensor_packet=packet)
                    self._latest=CapturedFrame(image,metadata,tick,self._sequence,jpeg);self._condition.notify_all()
            except Exception as exc:
                with self._condition:
                    self._connected=False;self._error='synchronized_capture_failed:'+type(exc).__name__
                    self._condition.notify_all()
                self._stop.wait(float(self.config.get('reconnect_delay_s',1.0)))
            self._stop.wait(max(0.,interval-(time.monotonic()-started)))
        with self._condition:
            self._connected=False
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
                "capture_time_available": self._capture_clock_known,
                "capture_clock": "esp_timer" if self._capture_clock_known else None,
                "configured_source": self.config.get("source_id", "esp32-camera"),
            }

    def close(self):
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        if self._thread:
            self._thread.join(float(self.config.get("network_timeout_s", 5.0)) + 1.0)

