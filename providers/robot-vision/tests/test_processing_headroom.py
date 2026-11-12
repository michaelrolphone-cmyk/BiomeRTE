import threading
from unittest.mock import patch
import cv2
import unittest
from types import SimpleNamespace
import test_vision_service as fixtures
from processing_performance import ProcessingPerformance
from detector import ObjectDetector

class HeadroomTests(unittest.TestCase):
    setUp = fixtures.VisionServiceTests.setUp
    tearDown = fixtures.VisionServiceTests.tearDown
    make_service = fixtures.VisionServiceTests.make_service
    frame = fixtures.VisionServiceTests.frame
    process = fixtures.VisionServiceTests.process

    def test_detection_and_tracking_overlap_but_publish_same_frame(self):
        service = self.make_service()
        entered, release = threading.Event(), threading.Event()
        detect, track = service.detector.detect, service.backend.track
        def slow_detect(*args):
            entered.set()
            if not release.wait(2):
                raise AssertionError("tracking never overlapped detection")
            return detect(*args)
        def tracking(*args):
            self.assertTrue(entered.wait(2))
            release.set()
            return track(*args)
        service.detector.detect, service.backend.track = slow_detect, tracking
        captured = self.frame(service, "overlap")
        snapshot = self.process(service, captured)
        self.assertEqual(service.detector.calls, [captured.metadata["frame_id"]])
        self.assertEqual(snapshot["localization"]["frame_id"], captured.metadata["frame_id"])
        self.assertTrue(snapshot["localization"]["valid"])

    def test_source_jpeg_avoids_reencoding_the_same_frame(self):
        service = self.make_service()
        captured = self.frame(service, "source-jpeg")
        captured.jpeg_bytes = cv2.imencode(".jpg", captured.image)[1].tobytes()
        with patch("cv2.imencode", side_effect=AssertionError("unnecessary JPEG encoding")):
            snapshot = self.process(service, captured)
        self.assertEqual(snapshot["localization"]["frame_id"], captured.metadata["frame_id"])

    def test_anchor_lookup_is_exact_and_retains_historical_tombstones(self):
        service = self.make_service()
        self.process(service, self.frame(service, "anchor"))
        original = service.revision
        with patch.object(service.store, "read_map", side_effect=AssertionError("whole-map scan")):
            self.assertEqual(service._sonar_anchor(service.map_id, "kf-0")["id"],"kf-0")
        result = service.store.apply_map_update(service.map_id, original,
            [{"op":"delete","kind":"keyframe","entity_id":"kf-0"}],reason="test")
        service.revision = result["revision"] if isinstance(result,dict) else int(result)
        self.assertIsNone(service.store.read_entity(service.map_id,"keyframe","kf-0"))
        self.assertIsNotNone(service.store.read_entity(service.map_id,"keyframe","kf-0",original))
        self.assertIsNone(service.store.read_entity(service.map_id,"keyframe","missing"))

    def test_serial_mode_remains_available_for_comparison(self):
        self.config["parallel_detection"] = False
        service = self.make_service()
        completed = []
        detect, track = service.detector.detect, service.backend.track
        def detection(*args):
            value = detect(*args)
            completed.append(True)
            return value
        def tracking(*args):
            self.assertEqual(completed, [True])
            return track(*args)
        service.detector.detect, service.backend.track = detection, tracking
        self.process(service, self.frame(service))
        self.assertIsNone(service._detection_executor)

class PerformanceTests(unittest.TestCase):
    def test_budget_gaps_loss_and_retention_are_measured(self):
        perf = ProcessingPerformance(5, capacity=2)
        for i, ms in enumerate([100, 120, 300]):
            captured = SimpleNamespace(metadata={"measurement_monotonic_s": 10+i*.4},
                                       decoded_monotonic=10+i*.4)
            value = perf.record(ms, {"tracking": ms, "optional": None},
                                captured, i*2, i != 2, now=10.2+i*.4)
        self.assertEqual(value["window_frames"], 2)
        self.assertEqual(value["processing_ms_p95"], 300)
        self.assertEqual(value["frame_budget_headroom_fraction_p95"], -.5)
        self.assertEqual(value["skipped_camera_frames_window"], 2)
        self.assertEqual(value["invalid_localization_frames_window"], 1)
        self.assertAlmostEqual(value["processed_capture_gap_ms_p95"], 400)
        self.assertEqual(value["stage_ms_p95"]["optional"], None)

    def test_batched_detector_keeps_object_identity_and_velocity(self):
        transfers = []
        class Tensor:
            def __init__(self, data): self.data = data
            def detach(self): return self
            def cpu(self): transfers.append(True); return self
            def tolist(self): return self.data
        boxes = SimpleNamespace(xyxy=Tensor([[1.,2.,5.,6.]]), cls=Tensor([0]),
                                conf=Tensor([.9]), id=Tensor([7]))
        result = SimpleNamespace(boxes=boxes, names={0:"object"})
        detector = ObjectDetector({}, ".")
        detector.model = SimpleNamespace(track=lambda *a, **kw: [result])
        detector.device = "mps"
        value = detector.detect(None, "f1", 1.)
        self.assertEqual(value["availability"], "available")
        self.assertEqual(value["items"][0]["tracker_local_id"], 7)
        self.assertEqual(value["items"][0]["confidence"], .9)
        self.assertEqual(len(transfers), 4)
        value = detector.detect(None, "f2", 2.)
        self.assertEqual(value["items"][0]["image_motion"]["velocity_px_per_s"], [0.,0.])
