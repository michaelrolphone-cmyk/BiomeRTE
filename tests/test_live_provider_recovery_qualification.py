import json
import struct
import unittest

from tests.qualify_live_provider_recovery import CONSUMERS, FEEDS, assess, feed_record


class LiveProviderRecoveryQualification(unittest.TestCase):
    def setUp(self):
        self.evidence = {
            "submission": {"status": 200, "operation": "vision-check", "lease": "lease-1"},
            "results": [{"status": 200, "operation": "vision-check", "lease": "lease-1",
                         "state": "succeeded", "has_result": True}],
            "lease_check": {"operation": "vision-check", "lease": "lease-1", "state": "released"},
            "streams": {consumer: {"seconds": 80, "feeds": {
                feed: {"records": 80, "max_gap_seconds": 0.9,
                       "max_delivery_age_seconds": 0.5, "unclocked_records": 0} for feed in FEEDS}}
                for consumer in CONSUMERS},
        }

    def test_requires_successful_result_for_the_submitted_lease(self):
        self.assertEqual(assess(self.evidence, 80, 1, 1), [])
        self.evidence["results"][0]["lease"] = "unrelated"
        self.assertIn("the normal signed API did not return the completed result for this operation and lease",
                      assess(self.evidence, 80, 1, 1))

    def test_recovered_receipt_cannot_hide_gateway_submission_failure(self):
        self.evidence["submission"].update(status=503, lease=None)
        failures = assess(self.evidence, 80, 1, 1)
        self.assertEqual(failures, ["signed submission did not confirm an operation and lease (HTTP 503)"])

    def test_production_errors_and_stream_stalls_cannot_pass(self):
        self.evidence["results"].append({"status": 429})
        self.evidence["results"].append({"status": 503})
        self.evidence["lease_check"]["state"] = "fencing"
        self.evidence["streams"]["public"]["feeds"]["frame:rear"]["records"] = 0
        self.evidence["streams"]["bridge"]["feeds"]["sensor_tick:front"]["max_gap_seconds"] = 9
        self.evidence["streams"]["public"]["feeds"]["sensor_tick:front"]["max_delivery_age_seconds"] = 9
        failures = assess(self.evidence, 80, 1, 1)
        self.assertIn("signed result read returned HTTP 429", failures)
        self.assertIn("signed result read returned HTTP 503", failures)
        self.assertIn("release of the exact lease was not observed", failures)
        self.assertIn("public frame:rear delivered fewer than two distinct records", failures)
        self.assertIn("bridge sensor_tick:front exceeded the 1s continuity budget (9s)", failures)
        self.assertIn("public sensor_tick:front failed the 1s delivery-age check (9s)", failures)

    def test_manifest_and_empty_image_are_not_live_frames(self):
        def packet(metadata, body=b""):
            header = json.dumps(metadata).encode()
            return struct.pack("!I", len(header)) + header + body

        self.assertIsNone(feed_record(packet({"type": "sensor_manifest", "camera": "front"})))
        self.assertIsNone(feed_record(packet({"type": "frame", "camera": "front"})))
        self.assertIsNone(feed_record(packet({"type": "frame", "camera": "front"}, b"anonymous")))
        frame = packet({"type": "frame", "camera": "front", "frame_id": "front-boot-1"}, b"image")
        self.assertEqual(feed_record(frame)[0], "frame:front")
        self.assertEqual(feed_record(frame), feed_record(frame))

    def test_rewrapping_one_camera_or_sensor_observation_does_not_advance_the_feed(self):
        def identity(metadata, image=b""):
            data = json.dumps(metadata).encode()
            return feed_record(struct.pack("!I", len(data)) + data + image)[1]
        frame = {"type": "frame", "camera": "front", "frame_id": "camera-boot:12"}
        self.assertEqual(identity(frame, b"raw"), identity(dict(frame, sequence=95, processed_monotonic=320), b"reencoded"))
        tick = {"type": "sensor_tick", "camera": "rear", "sensor_packet":
                {"boot_id": "boot", "receiver_id": "rear-device", "capture_monotonic_us": 100}}
        self.assertEqual(identity(tick), identity(dict(tick, frame_id="adapter:99", sequence=99, processed_monotonic=340)))
        self.assertNotEqual(identity(tick), identity(dict(tick, sensor_packet=dict(tick["sensor_packet"], boot_id="new-boot"))))

    def test_camera_descriptor_does_not_mask_the_wire_camera_id(self):
        metadata = {"type": "frame", "camera": {"lens": "calibrated"},
                    "camera_id": "rear", "frame_id": "rear-boot:42"}
        raw = json.dumps(metadata).encode()
        value = feed_record(struct.pack("!I", len(raw)) + raw + b"jpeg")
        self.assertIsNotNone(value)
        self.assertEqual(value[0], "frame:rear")
