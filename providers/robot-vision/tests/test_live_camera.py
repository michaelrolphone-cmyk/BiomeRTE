"""The capture lane must advance while real service processing is blocked."""
import copy
import asyncio
import io
import json
from pathlib import Path
import queue
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from live_camera import LiveCamera
from leased_publish import LiveFrames
from vision_service import VisionService
from vision_config import DEFAULT_CONFIG
from test_vision_service import FakeDetector, BackendFactory, calibration


class Camera:
    def __init__(self):
        self.condition = threading.Condition()
        self.latest = None

    def next_frame(self, after):
        with self.condition:
            self.condition.wait_for(lambda: self.latest is not None and self.latest.sequence > after,
                                    timeout=.03)
            return self.latest if self.latest is not None and self.latest.sequence > after else None

    def offer(self, frame):
        with self.condition:
            self.latest = frame
            self.condition.notify_all()

    def close(self):
        pass

    def status(self):
        return {'state': 'fixture'}


def frame(sequence, value=60):
    image = np.full((24, 32, 3), value, np.uint8)
    ok, encoded = cv2.imencode('.jpg', image)
    assert ok
    return SimpleNamespace(sequence=sequence, image=image, jpeg_bytes=encoded.tobytes(),
                           decoded_monotonic=time.monotonic(), metadata={
                               'frame_id': 'capture:' + str(sequence), 'sequence': sequence,
                               'stream_epoch': 'capture', 'width': 32, 'height': 24,
                               'capture_time': None, 'receive_decode_time': '2026-11-11T00:00:00+00:00'})


def unpack(value):
    assert struct.unpack('!I', value[:4])[0] == len(value)-4
    length = struct.unpack('!I', value[4:8])[0]
    return json.loads(value[8:8+length]), value[8+length:]


class LiveCameraTests(unittest.TestCase):
    def test_processing_never_publishes_an_old_image_and_queue_keeps_latest(self):
        camera, output = Camera(), LiveFrames()
        worker = LiveCamera(camera, output, lambda f, p: {
            'frame_id': f.metadata['frame_id'], 'analysis_frame_id': p.get('frame_id')})
        with output.subscribe() as inbox:
            worker.start()
            try:
                first, second = frame(1, 40), frame(2, 90)
                camera.offer(first)
                self.assertEqual(unpack(inbox.get(timeout=1))[1], first.jpeg_bytes)
                camera.offer(second)
                self.assertEqual(unpack(inbox.get(timeout=1))[1], second.jpeg_bytes)
                worker.record_processed(first, {'frame_id': 'capture:1'})
                with self.assertRaises(queue.Empty):
                    inbox.get(timeout=.05)
                # A slow consumer receives one newest frame, never a backlog.
                camera.offer(frame(3))
                end = time.monotonic()+1
                while worker.status()['captures_published'] < 3 and time.monotonic() < end:
                    time.sleep(.005)
                newest = frame(4)
                camera.offer(newest)
                end = time.monotonic()+1
                while worker.status()['captures_published'] < 4 and time.monotonic() < end:
                    time.sleep(.005)
                meta, jpeg = unpack(inbox.get(timeout=1))
                self.assertEqual(meta['frame_id'], 'capture:4')
                self.assertEqual(meta['analysis_frame_id'], 'capture:1')
                self.assertEqual(jpeg, newest.jpeg_bytes)
            finally:
                worker.close()

    def test_stale_processing_result_cannot_replace_newer_analysis(self):
        worker = LiveCamera(Camera(), LiveFrames(), lambda f, p: {})
        self.assertTrue(worker.record_processed(frame(5), {'frame_id': 'capture:5'}))
        self.assertFalse(worker.record_processed(frame(2), {'frame_id': 'capture:2'}))
        self.assertEqual(worker._processed_metadata['frame_id'], 'capture:5')

    def test_original_jpeg_is_forwarded_without_encoding_or_image_access(self):
        captured = SimpleNamespace(jpeg_bytes=b'exact-sdk-jpeg')
        self.assertIs(LiveCamera._encode(captured), captured.jpeg_bytes)

    def test_service_camera_advances_while_backend_and_pipeline_lock_are_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            config = copy.deepcopy(DEFAULT_CONFIG)
            config['camera']['url'] = 'fixture://no-network'
            config['calibration'] = calibration()
            camera = Camera()
            service = VisionService(config, directory, camera=camera, detector=FakeDetector(),
                                    backend_factory=BackendFactory())
            entered, release = threading.Event(), threading.Event()
            original = service.backend.track
            def blocked(*args):
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('test did not release backend')
                return original(*args)
            service.backend.track = blocked
            service._live_camera = LiveCamera(camera, service.live_frames, service._raw_live_metadata).start()
            first = frame(1, 40)
            errors = []
            def process():
                try:
                    with service._pipeline_lock:
                        service.process_frame(first)
                except Exception as exc:
                    errors.append(exc)
            processing = threading.Thread(target=process)
            try:
                with service.live_frames.subscribe() as inbox:
                    processing.start()
                    self.assertTrue(entered.wait(1))
                    camera.offer(first)
                    self.assertEqual(unpack(inbox.get(timeout=1))[0]['frame_id'], 'capture:1')
                    second = frame(2, 95)
                    camera.offer(second)
                    meta, jpeg = unpack(inbox.get(timeout=1))
                    self.assertEqual(meta['frame_id'], 'capture:2')
                    self.assertEqual(jpeg, second.jpeg_bytes)
                    self.assertFalse(meta['pose']['valid'])
                    self.assertEqual(meta['detections'], [])
                    self.assertEqual(meta['processing_state'], 'pending')
                    self.assertEqual(meta['processed_monotonic'], second.decoded_monotonic)
                    self.assertEqual(meta['processed_monotonic_basis'], 'camera_receive_decode_time')
                    self.assertFalse(release.is_set())
                    release.set()
                    processing.join(1)
                    self.assertFalse(processing.is_alive())
                    self.assertEqual(errors, [])
                    with self.assertRaises(queue.Empty):
                        inbox.get(timeout=.05)
                    third = frame(3)
                    camera.offer(third)
                    meta, jpeg = unpack(inbox.get(timeout=1))
                    self.assertEqual(meta['frame_id'], 'capture:3')
                    self.assertEqual(meta['analysis']['frame_id'], 'capture:1')
                    self.assertEqual(meta['analysis']['pose']['frame_id'], 'capture:1')
                    self.assertEqual(jpeg, third.jpeg_bytes)
            finally:
                release.set()
                if processing.ident is not None:
                    processing.join(2)
                service.close()

    def test_camera_packet_survives_actual_leased_sender_freshness_and_router_checks(self):
        from leased_publish import packet
        from leased_streams import VisionStreams
        from biomerte.stream_router import Session, decode
        import test_stream_sequence_recovery as recovery
        with tempfile.TemporaryDirectory() as directory:
            config = copy.deepcopy(DEFAULT_CONFIG)
            config['camera']['url'] = 'fixture://no-network'
            config['calibration'] = calibration()
            service = VisionService(config, directory, camera=Camera(), detector=FakeDetector(),
                                    backend_factory=BackendFactory())
            try:
                captured = frame(1)
                metadata = service._raw_live_metadata(captured, {})
                class Response(io.BytesIO):
                    status = 200
                response = Response(packet(metadata, captured.jpeg_bytes))
                class Local:
                    def request(self, *args):
                        pass
                    def getresponse(self):
                        return response
                    def abort(self):
                        response.close()
                binding, stop, received = recovery.grant(), threading.Event(), []
                router = Session(binding)
                class Wire:
                    async def send(self, message):
                        router.check('provider', 'vision', message)
                        received.append(decode(message, 4194304))
                        stop.set()
                    async def __aenter__(self):
                        return self
                    async def __aexit__(self, *args):
                        return False
                class Remote:
                    def call(self, *args):
                        return {'url': 'ws://127.0.0.1:8751/session-a',
                                'attachment_credential': 'fixture'}
                publisher = VisionStreams(Remote(), 'fixture')
                async def run():
                    with patch('leased_streams.LocalConnection', return_value=Local()), \
                         patch('websockets.asyncio.client.connect', return_value=Wire()):
                        await asyncio.wait_for(publisher.run(binding, stop), 2)
                asyncio.run(run())
                self.assertEqual(len(received), 1)
                meta, jpeg = received[0]
                self.assertEqual(jpeg, captured.jpeg_bytes)
                self.assertEqual(meta['frame_id'], captured.metadata['frame_id'])
                self.assertEqual(meta['processed_monotonic'], captured.decoded_monotonic)
                self.assertEqual(meta['processing_state'], 'pending')
            finally:
                service.close()


if __name__ == '__main__':
    unittest.main()
