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

    def test_frame_contains_complete_firmware_ahrs_and_cached_radio_observations(self):
        service = self.make_service()
        firmware = {"type":"ahrs", "receiver_id":"camera", "boot_id":"boot",
                    "sequence":7, "timestamp_ms":1800000000000,
                    "ahrs":{"worker_ready":True, "paused":False,
                            "attitude":{"roll_deg":12.5}, "imu":{"accel_m_s2":[0,0,9.8]}}}
        cached = {"telemetry":firmware, "received_at":"2026-11-10T20:00:00Z",
                  "received_monotonic":123.0}
        radio = {"enabled":True, "telemetry":{"observations":[{"kind":"wifi","rssi_dbm":-51}]}}
        with patch.object(service.ahrs, "snapshot", return_value=copy.deepcopy(cached)) as snapshot, \
             patch.object(service.radio, "status", return_value=radio) as status:
            with service.live_frames.subscribe() as inbox:
                self.process(service, self.frame(service, "sensors"))
                value = inbox.get(timeout=1)
            size = int.from_bytes(value[:4], "big")
            metadata, image = decode(value[4:4+size], 4194304)
            self.assertEqual(metadata["ahrs"]["ahrs"], firmware["ahrs"])
            self.assertEqual(metadata["ahrs"]["boot_id"], "boot")
            self.assertEqual(metadata["ahrs"]["snapshot"]["received_monotonic"], 123.0)
            self.assertEqual(metadata["radio"]["telemetry"]["observations"][0]["rssi_dbm"], -51)
            self.assertTrue(any(call.kwargs.get("include_observations") is True for call in status.call_args_list))
            self.assertTrue(snapshot.called)
            self.assertTrue(image)

    def test_missing_sensor_cache_preserves_honest_health_without_fabricating_values(self):
        service = self.make_service()
        with patch.object(service.ahrs, "snapshot", return_value=None), \
             patch.object(service.ahrs, "status", return_value={"enabled":True,"state":"unobserved"}):
            self.assertEqual(service._stream_ahrs(), {"enabled":True,"state":"unobserved"})


if __name__ == "__main__":
    unittest.main()

class DenseMotionTelemetryTests(unittest.TestCase):
    def test_dense_history_and_events_fit_and_round_trip(self):
        import json
        from stream_telemetry import bounded_metadata,size
        from leased_publish import packet
        rows=[dict(monotonic_us=10000000+i*20000,sample_count=i,generation=1,
            valid=True,attitude_valid=True,accel_m_s2=[.01,-9.8,.02],
            gyro_rad_s=[.001,.002,.003],quaternion_wxyz=[.707,-.707,0,0],yaw_deg=.1)
            for i in range(32)]
        events=[dict(event_id=str(i),type='turning',evidence={'heading_delta_deg':.6},
            message='Observed left turn',sequence=i) for i in range(64)]
        metadata=dict(ahrs=dict(ahrs=dict(imu_samples=rows,imu={'valid':True})),
            radio={},ultrasonic={},motion_events=dict(events=events,last_sequence=63,state={'available':True}),
            events=events,frame_id='capture:1')
        result=bounded_metadata(metadata)
        self.assertLessEqual(size(result),15872)
        history=result['ahrs']['ahrs']['imu_history']
        self.assertEqual(len(history['rows']),32)
        self.assertEqual(history['rows'][-1],[10620000,31,1,True,True,.01,-9.8,.02,.001,.002,.003,.707,-.707,0,0,.1])
        self.assertEqual(result['events'][-1]['event_id'],'63')
        self.assertEqual(result['event_delivery']['recent_events_omitted'],56)
        self.assertIn('imu_samples',metadata['ahrs']['ahrs']) # never mutate source evidence
        payload=packet(metadata,b'jpeg')
        length=int.from_bytes(payload[:4],'big')
        decoded,pixels=decode(payload[4:4+length],4194304)
        self.assertEqual(decoded['frame_id'],'capture:1');self.assertEqual(pixels,b'jpeg')
