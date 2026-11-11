"""Publish only the newest capture, independently of vision processing.

Processing updates a separate, explicitly frame-bound analysis reference.
It never publishes an image or delays the camera's latest-frame consumer.
"""
import threading
import time


class LiveCamera:
    def __init__(self, camera, output, metadata, *, encode=None):
        self.camera, self.output, self.metadata = camera, output, metadata
        self.encode = encode or self._encode
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._sequence = -1
        self._processed_sequence = -1
        self._last_processed = None
        self._processed_metadata = {}
        self._counts = dict(processed_seen=0, captures_published=0,
                            older_results_discarded=0)
        self._error = None

    @staticmethod
    def _encode(captured):
        jpeg = getattr(captured, 'jpeg_bytes', None)
        if jpeg is not None:
            return jpeg
        import cv2
        ok, image = cv2.imencode('.jpg', captured.image, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            raise RuntimeError('live_camera_encoding_failed')
        return image.tobytes()

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name='live-camera', daemon=True)
            self._thread.start()
        return self

    def record_processed(self, captured, metadata):
        with self._lock:
            self._counts['processed_seen'] += 1
            if captured.sequence < self._processed_sequence:
                self._counts['older_results_discarded'] += 1
                return False
            self._last_processed = time.monotonic()
            self._processed_metadata = dict(metadata)
            self._processed_sequence = captured.sequence
            return True

    def _publish(self, captured, metadata, image):
        # Called with the publication lock. CameraSource sequences are global
        # for its lifetime, including reconnects and device boot changes.
        if self._stop.is_set():
            return False
        if captured.sequence <= self._sequence:
            self._counts['older_results_discarded'] += 1
            return False
        if metadata.get('frame_id') != captured.metadata['frame_id']:
            raise ValueError('live_camera_frame_identity_mismatch')
        self.output.publish(metadata, image)
        self._sequence = captured.sequence
        self._counts['captures_published'] += 1
        self._error = None
        return True

    def _run(self):
        sequence = 0
        while not self._stop.is_set():
            try:
                captured = self.camera.next_frame(sequence)
                if captured is None:
                    continue
                sequence = captured.sequence
                image = self.encode(captured)
                with self._lock:
                    previous = dict(self._processed_metadata)
                # No pipeline/backend/store lock is taken by this worker.
                metadata = self.metadata(captured, previous)
                with self._lock:
                    self._publish(captured, metadata, image)
            except Exception as exc:
                with self._lock:
                    self._error = type(exc).__name__ + ':' + str(exc)[:180]
                self._stop.wait(0.1)

    def status(self):
        with self._lock:
            return dict(self._counts, error=self._error, independent_of_processing=True,
                        last_processed_age_s=None if self._last_processed is None
                        else time.monotonic() - self._last_processed)

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
