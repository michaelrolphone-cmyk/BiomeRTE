"""Firmware cache -> vision frame metadata, without HTTP in frame delivery."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import test_vision_service as fixtures
from test_ultrasonic_source import packet
from biomerte.stream_router import decode


class UltrasonicStreamTests(unittest.TestCase):
    setUp = fixtures.VisionServiceTests.setUp
    tearDown = fixtures.VisionServiceTests.tearDown
    make_service = fixtures.VisionServiceTests.make_service
    frame = fixtures.VisionServiceTests.frame
    process = fixtures.VisionServiceTests.process

    def test_four_cached_ranges_reach_live_frame_without_new_camera_request(self):
        service = self.make_service()
        cached = {"telemetry": packet(), "received_at": "receipt",
                  "elapsed_since_receive_s": .1, "request_duration_s": .02,
                  "observations": [{"id": name, "effective_age_ms": 150}
                                   for name in ("front", "left", "right", "rear")]}
        with patch.object(service.ultrasonic, "snapshot", return_value=copy.deepcopy(cached)), \
             patch.object(service.ultrasonic.client, "get_ultrasonic", side_effect=AssertionError("HTTP in frame path")):
            with service.live_frames.subscribe() as inbox:
                self.process(service, self.frame(service, "ranges"))
                value = inbox.get(timeout=1)
            size = int.from_bytes(value[:4], "big")
            metadata, image = decode(value[4:4+size], 4194304)
            self.assertEqual(metadata["ultrasonic"]["ultrasonic"], packet()["ultrasonic"])
            self.assertEqual(metadata["ultrasonic"]["snapshot"]["observations"][0]["effective_age_ms"], 150)
            self.assertEqual(metadata["ultrasonic"]["boot_id"], "boot")
            self.assertTrue(image)

    def test_unflashed_firmware_keeps_explicit_unavailable_health(self):
        service = self.make_service()
        with patch.object(service.ultrasonic, "snapshot", return_value=None):
            value = service._stream_ultrasonic()
            self.assertNotIn("ultrasonic", value)
            self.assertIn("state", value)


if __name__ == "__main__": unittest.main()
