"""Exercise the production spawned ingress over authenticated WebSockets."""
import asyncio
import json
import queue
import socket
import struct
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from sensor_push import SensorPushReceiver


class IsolatedIngressTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, include_radio):
        from websockets.asyncio.client import connect
        images, sensors = queue.Queue(), queue.Queue()
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            token = "isolated-ingress-test-token-0123456789"
            state.joinpath("sensor-push.json").write_text(json.dumps({
                "listen": "127.0.0.1", "port": port, "token": token,
                "receiver_id": "camera-test",
            }))
            service = SimpleNamespace(
                state_dir=state,
                config={"radio": {"enabled": include_radio}, "freshness_threshold_s": 2},
                camera=SimpleNamespace(push_jpeg=lambda *args: images.put(args)),
            )
            stream = SimpleNamespace(service=service, error=None,
                                     publish=lambda *args, **kwargs: sensors.put((args, kwargs)))
            receiver = SensorPushReceiver(stream)
            receiver.start()
            try:
                deadline = time.monotonic() + 10
                while True:
                    self.assertTrue(receiver.process.is_alive(), stream.error)
                    status = state / "sensor-push-status.json"
                    if status.exists() and json.loads(status.read_text()).get("state") == "listening":
                        break
                    if time.monotonic() >= deadline:
                        self.fail("spawned sensor ingress did not listen")
                    await asyncio.sleep(.01)
                headers = {"Authorization": "Sensor " + token}
                async with connect("ws://127.0.0.1:" + str(port) + "/camera",
                                   additional_headers=headers) as camera:
                    clock = json.loads(await asyncio.wait_for(camera.recv(), 3))
                    self.assertEqual(clock["type"], "clock")
                    await camera.send(json.dumps({
                        "type": "clock", "id": clock["id"], "boot_id": "camera-boot",
                        "received_monotonic_us": 1000000, "served_monotonic_us": 1000000,
                    }))
                    ready = json.loads(await asyncio.wait_for(camera.recv(), 3))
                    self.assertEqual(ready, {"type": "camera_ready", "boot_id": "camera-boot"})
                    metadata = {
                        "schema": "biome.capture.v1", "receiver_id": "camera-test",
                        "boot_id": "camera-boot", "camera_id": "front",
                        "frame_id": "camera-boot:1000000", "capture_monotonic_us": 1000000,
                        "served_monotonic_us": 1000001, "capture_clock": "esp_timer",
                        "width": 2, "height": 2,
                        "radio": {"schema": 1, "receiver_id": "camera-test",
                                  "boot_id": "camera-boot", "uptime_ms": 1000,
                                  "sequence": 1, "wifi": [], "ble": []},
                    }
                    encoded = json.dumps(metadata).encode()
                    jpeg = bytes.fromhex("ffd8ffe0ffd9")
                    await camera.send(struct.pack("!I", len(encoded)) + encoded + jpeg)
                    ack = json.loads(await asyncio.wait_for(camera.recv(), 3))
                    self.assertEqual(ack, {"type": "camera_ack", "frame_id": metadata["frame_id"]})
                    delivered, image, interval, received = await asyncio.to_thread(images.get, True, 3)
                    self.assertEqual(delivered["frame_id"], metadata["frame_id"])
                    self.assertEqual(image, jpeg)
                    self.assertLessEqual(interval[0], interval[1])
                    self.assertLessEqual(interval[1], received)
                    if include_radio:
                        self.assertEqual(delivered["radio"], metadata["radio"])
                    else:
                        self.assertNotIn("radio", delivered)

                # Verify the same child still delivers independent sensor ticks.
                async with connect("ws://127.0.0.1:" + str(port) + "/sensors",
                                   additional_headers=headers) as sensor:
                    clock = json.loads(await asyncio.wait_for(sensor.recv(), 3))
                    await sensor.send(json.dumps({
                        "type": "clock", "id": clock["id"], "boot_id": "sensor-boot",
                        "received_monotonic_us": 2000000, "served_monotonic_us": 2000000,
                    }))
                    tick = {
                        "schema": "biome.sensor_tick.v1", "receiver_id": "camera-test",
                        "boot_id": "sensor-boot", "frame_id": "sensor-boot:2000000",
                        "capture_monotonic_us": 2000000, "served_monotonic_us": 2000001,
                        "capture_clock": "esp_timer", "stream_sequence": 1,
                        "image_available": False,
                        "capture_timestamp_semantics": "sensor_snapshot_tick_no_camera_frame",
                    }
                    await sensor.send(json.dumps(tick))
                    ack = json.loads(await asyncio.wait_for(sensor.recv(), 3))
                    self.assertEqual(ack["type"], "ack")
                    self.assertEqual(ack["stream_sequence"], 1)
                    args, kwargs = await asyncio.to_thread(sensors.get, True, 3)
                    self.assertEqual(args[0]["frame_id"], tick["frame_id"])
                    self.assertFalse(args[0]["image_available"])
                    self.assertIn("capture_interval", kwargs)
                self.assertTrue(receiver.process.is_alive())
                self.assertIsNone(stream.error)
            finally:
                await asyncio.to_thread(receiver.close)
            self.assertFalse(receiver.process.is_alive())

    async def test_spawned_worker_preserves_enabled_radio_and_delivers_sensors(self):
        await self.exercise(True)

    async def test_spawned_worker_delivers_camera_and_sensors_with_radio_disabled(self):
        await self.exercise(False)


if __name__ == "__main__":
    unittest.main()
