import asyncio
import json
import struct
import time
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from camera_push import receive_camera
from camera_source import CameraSource
from sensor_push import ClockAlignment


class CameraPushTest(unittest.IsolatedAsyncioTestCase):
    async def test_both_cameras_receive_real_jpegs_without_http(self):
        async def exercise(role):
            receiver_id = role + '-test'
            camera = CameraSource({'device_id': receiver_id, 'synchronized_capture': True, 'source_id': role})
            accepted = []
            def deliver(*args):
                accepted.append(args)
                camera.push_jpeg(*args)
            async def receive(socket):
                await receive_camera(socket, receiver_id, role, ClockAlignment(), deliver)
            with patch.object(camera, '_run_synchronized', side_effect=AssertionError('HTTP fetch')) as http:
                camera.start()
                try:
                    async with serve(receive, '127.0.0.1', 0) as server:
                        port = server.sockets[0].getsockname()[1]
                        async with connect(f'ws://127.0.0.1:{port}/camera') as client:
                            clock = json.loads(await client.recv())
                            t = time.monotonic_ns() // 1000
                            await client.send(json.dumps(dict(type='clock',id=clock['id'],boot_id='boot',
                                received_monotonic_us=t,served_monotonic_us=t)))
                            self.assertEqual(json.loads(await client.recv())['type'], 'camera_ready')
                            jpeg = cv2.imencode('.jpg',np.zeros((24,32,3),np.uint8))[1].tobytes()
                            for i in range(2):
                                t = time.monotonic_ns() // 1000
                                meta = dict(schema='biome.capture.v1',receiver_id=receiver_id,boot_id='boot',
                                    frame_id='boot:'+str(t),capture_clock='esp_timer',capture_monotonic_us=t,
                                    served_monotonic_us=t,camera_id=role,width=32,height=24)
                                encoded = json.dumps(meta).encode()
                                await client.send(struct.pack('!I',len(encoded))+encoded+jpeg)
                                self.assertEqual(json.loads(await client.recv())['frame_id'],meta['frame_id'])
                                frame = await asyncio.to_thread(camera.next_frame,i,2)
                                self.assertEqual(frame.jpeg_bytes,jpeg)
                                self.assertEqual(frame.metadata['capture_monotonic_us'],t)
                                self.assertEqual(frame.metadata['sensor_packet']['camera_id'],role)
                            self.assertEqual(camera.status()['capture_transport'],'websocket_push')
                            http.assert_not_called()
                finally:
                    camera.close()
        await asyncio.gather(exercise('front'),exercise('rear'))


class EnrollmentTest(unittest.TestCase):
    def test_disconnect_preserves_enrollment_and_route_change_reprovisions_once(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from threading import Lock
        from types import SimpleNamespace
        from unittest.mock import Mock
        from rear_sensor_push import RearSensorIngress

        with TemporaryDirectory() as directory:
            ingress = RearSensorIngress.__new__(RearSensorIngress)
            ingress.service = SimpleNamespace(state_dir=Path(directory))
            ingress.rig = SimpleNamespace(config={"rear":{"camera":{}}})
            ingress.config = dict(receiver_id="enrolled", gateway_host="first", port=9878, token="sample")
            ingress.stop = Mock()
            ingress.stop.wait.return_value = False
            ingress.lock = Lock()
            ingress.latest = {"packet":{}}
            with patch("camera_telemetry.CameraHTTPClient") as client:
                client.return_value._request.return_value = dict(receiver_id="enrolled", camera_id="rear")
                ingress._configure_device()
                ingress.latest = None
                ingress._configure_device()
                client.assert_not_called()
                ingress.config["gateway_host"] = "changed"
                ingress._configure_device()
                self.assertEqual(client.return_value._request.call_count, 2)
                ingress._configure_device()
                self.assertEqual(client.return_value._request.call_count, 2)
