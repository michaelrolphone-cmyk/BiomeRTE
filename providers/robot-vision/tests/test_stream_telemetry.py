"""Live frames contain cached sensor values, not health-only summaries."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import test_vision_service as fixtures
from biomerte.stream_router import decode


class StreamTelemetryTests(unittest.TestCase):
    setUp = fixtures.VisionServiceTests.setUp
    tearDown = fixtures.VisionServiceTests.tearDown
    make_service = fixtures.VisionServiceTests.make_service
    frame = fixtures.VisionServiceTests.frame
    process = fixtures.VisionServiceTests.process

    def test_frame_contains_complete_firmware_ahrs_and_tof_solution(self):
        service = self.make_service()
        firmware = {"type":"ahrs", "receiver_id":"camera", "boot_id":"boot",
                    "sequence":7, "timestamp_ms":1800000000000,
                    "ahrs":{"worker_ready":True, "paused":False,
                            "attitude":{"roll_deg":12.5}, "imu":{"accel_m_s2":[0,0,9.8]}}}
        cached = {"telemetry":firmware, "received_at":"2026-11-10T20:00:00Z",
                  "received_monotonic":123.0}
        tof = {"schema":"rover.tof.solution/1","valid":False,"status":"incomplete_or_inconsistent_anchor_geometry","anchor_count":4}
        with patch.object(service.ahrs, "snapshot", return_value=copy.deepcopy(cached)) as snapshot, \
             patch("vision_service.read_tof", return_value=tof) as ranging:
            with service.live_frames.subscribe() as inbox:
                self.process(service, self.frame(service, "sensors"))
                value = inbox.get(timeout=1)
            size = int.from_bytes(value[:4], "big")
            metadata, image = decode(value[4:4+size], 4194304)
            self.assertEqual(metadata["ahrs"]["ahrs"], firmware["ahrs"])
            self.assertEqual(metadata["ahrs"]["boot_id"], "boot")
            self.assertEqual(metadata["ahrs"]["snapshot"]["received_monotonic"], 123.0)
            self.assertEqual(metadata["tof"], tof)
            self.assertNotIn("radio",metadata)
            self.assertTrue(ranging.called)
            self.assertTrue(snapshot.called)
            self.assertTrue(image)

    def test_missing_sensor_cache_preserves_honest_health_without_fabricating_values(self):
        service = self.make_service()
        with patch.object(service.ahrs, "snapshot", return_value=None), \
             patch.object(service.ahrs, "status", return_value={"enabled":True,"state":"unobserved"}):
            self.assertEqual(service._stream_ahrs(), {"enabled":True,"state":"unobserved"})


if __name__ == "__main__":
    unittest.main()
