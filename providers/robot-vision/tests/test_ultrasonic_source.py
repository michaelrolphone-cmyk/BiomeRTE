import copy
import json
import unittest
from camera_telemetry import CameraHTTPError
from ultrasonic_source import UltrasonicSource, validate_ultrasonic
from stream_telemetry import bounded_metadata


def packet():
    return {"schema": 1, "receiver_id": "camera", "boot_id": "boot", "uptime_ms": 100,
            "sequence": 1, "ultrasonic": {"schema": "hcsr04.v1", "paused": False,
            "worker_ready": True, "sensors": [{"id": name, "valid": True, "sequence": 1,
                "status": "ok", "distance_m": 1.0, "echo_us": 5882,
                "age_ms": 30, "observed_uptime_ms": 70}
                for name in ("front", "left", "right", "rear")]}}


class UltrasonicTests(unittest.TestCase):
    def source(self):
        self.clock = 100.0
        self.data = packet()
        owner = self
        class Client:
            def get_ultrasonic(self):
                owner.clock += .04
                return copy.deepcopy(owner.data)
        return UltrasonicSource({"source_id": "camera"}, client=Client(),
                                monotonic=lambda: self.clock, utc_clock=lambda: "receipt")

    def test_cached_reads_preserve_sample_age_and_add_request_uncertainty(self):
        source = self.source()
        self.assertTrue(source.poll_once())
        self.clock += .1
        cache = source.snapshot()
        self.assertAlmostEqual(cache["observations"][0]["effective_age_ms"], 170)
        self.assertEqual(cache["telemetry"]["ultrasonic"]["sensors"][0]["age_ms"], 30)
        self.assertFalse(source.poll_once())
        self.clock += 2
        self.assertFalse(source.snapshot()["fresh"])
        self.assertGreater(source.snapshot()["observations"][0]["effective_age_ms"], 2000)
        source.close()

    def test_faults_are_null_and_malformed_identity_is_rejected(self):
        for mutate in (lambda p: p["ultrasonic"]["sensors"][0].update(age_ms=None),
                       lambda p: p["ultrasonic"]["sensors"][0].update(distance_m=0),
                       lambda p: p["ultrasonic"]["sensors"][1].update(id="front"),
                       lambda p: p["ultrasonic"].update(paused=True)):
            p = packet(); mutate(p)
            with self.assertRaises(ValueError): validate_ultrasonic(p)
        p = packet()
        p["ultrasonic"]["sensors"][0].update(valid=False, status="timeout", distance_m=None, echo_us=None)
        self.assertIs(validate_ultrasonic(p), p)

    def test_old_firmware_does_not_break_or_fabricate_cached_measurements(self):
        source = self.source()
        def missing(): raise CameraHTTPError("not_found", http_status=404)
        source.client.get_ultrasonic = missing
        self.assertFalse(source.poll_once())
        self.assertIsNone(source.snapshot())
        self.assertFalse(source.status()["supported"])
        source.close()

    def test_stream_compaction_preserves_ranges_and_age_references(self):
        source = self.source(); source.poll_once(); cached = source.snapshot()
        wrapped = {**cached["telemetry"], "snapshot": cached}
        wrapped["snapshot"]["ignored_duplicate_diagnostics"] = "x" * 6000
        m = {"frame_id": "frame", "ultrasonic": wrapped,
             "radio": {"telemetry": {"telemetry": {"ultrasonic": packet()["ultrasonic"], "wifi": [], "ble": []}}}}
        bounded = bounded_metadata(m, 2500)
        self.assertLessEqual(len(json.dumps(bounded, separators=(",", ":")).encode()), 2500)
        self.assertEqual(len(bounded["ultrasonic"]["ultrasonic"]["sensors"]), 4)
        self.assertEqual(len(bounded["ultrasonic"]["cache_timing"]["observations"]), 4)
        self.assertNotIn("ultrasonic", bounded["radio"]["telemetry"]["telemetry"])
        source.close()


if __name__ == "__main__": unittest.main()
