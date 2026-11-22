import asyncio
import threading
import unittest
from types import SimpleNamespace

from rear_transport_preview import publish_sensor


class PreviewDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_lidar_preview_keeps_socket_loop_responsive_and_latest(self):
        loop = asyncio.get_running_loop()
        entered, release = threading.Event(), threading.Event()
        published = []
        completed = asyncio.Event()
        worker_observations = []

        def status():
            entered.set()
            worker_observations.append(release.wait(1))
            return {}

        def deliver(meta, _image):
            published.append(meta["frame_id"])
            if meta["frame_id"] == "rear:3":
                loop.call_soon_threadsafe(completed.set)

        service = SimpleNamespace(map_id="map", revision=1, session_id="test",
            lidar=SimpleNamespace(lock=threading.Lock(), raw_latest=None, status=status),
            rear_live_frames=SimpleNamespace(publish=deliver))
        ingress = SimpleNamespace(service=service, stop=threading.Event())
        def packet(tick):
            return dict(boot_id="rear", frame_id=f"rear:{tick}",
                        capture_monotonic_us=tick, lidar={"sensors": []})

        try:
            publish_sensor(ingress, packet(1), 1.)
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            publish_sensor(ingress, packet(2), 2.)
            publish_sensor(ingress, packet(3), 3.)
        finally:
            release.set()
        await asyncio.wait_for(completed.wait(), 2)
        self.assertEqual(worker_observations, [True, True])
        self.assertEqual(published, ["rear:1", "rear:3"])
