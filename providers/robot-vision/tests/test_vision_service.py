"""Injected integration tests. No camera, relay, SLAM model, or network is used."""
import base64
import copy
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vision_config import DEFAULT_CONFIG
from vision_service import VisionService


def identity_pose():
    return [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]


def calibration(identifier="synthetic-calibration-v1", validated=True):
    result = {
        "width": 32, "height": 24,
        "fx": 40.0 if validated else None, "fy": 40.0 if validated else None,
        "cx": 16.0 if validated else None, "cy": 12.0 if validated else None,
        "distortion": [0.0] * 5 if validated else None,
        "validated": validated, "provenance": "synthetic_unit_test_fixture",
        "mounting": {"height_m": 0.0635, "pitch_down_deg": 0.0, "roll_deg": 0.0,
                     "validated": validated, "provenance": "synthetic_unit_test_fixture"},
        "camera_to_robot": None,
    }
    if identifier is not None:
        result["calibration_id"] = identifier
    return result


class FakeCamera:
    def __init__(self):
        self.closed = False
        self.started = False

    def start(self):
        self.started = True

    def next_frame(self, _sequence):
        raise AssertionError("these deterministic tests must not run the capture thread")

    def status(self):
        return {"state": "synthetic_fixture", "network_used": False}

    def close(self):
        self.closed = True


class FakeDetector:
    def __init__(self):
        self.epoch = 0
        self.calls = []

    def start(self):
        return None

    def detect(self, image, frame_id, timestamp):
        self.calls.append(frame_id)
        return {
            "availability": "available", "model": "synthetic-test-detector",
            "source_frame_id": frame_id, "tracking_epoch": self.epoch,
            "items": [{
                "detection_id": "detection-" + frame_id,
                "class_name": "fixture-object", "confidence": 0.9,
                "bbox_xyxy_px": [2.0, 3.0, 10.0, 18.0],
                "track_id": "epoch-%s-track-1" % self.epoch,
                "motion_class": "unknown",
                "image_motion": {"availability": "unavailable", "reason": "test_fixture"},
                "position_map": None,
            }],
        }

    def reset_tracking(self):
        self.epoch += 1

    def status(self):
        return {"state": "ready", "model": "synthetic-test-detector", "tracking_epoch": self.epoch}


class BackendFactory:
    def __init__(self):
        self.instances = []
        self.fail_next_start = False

    def __call__(self, config, state_dir):
        instance = FakeBackend(config, state_dir, self)
        self.instances.append(instance)
        return instance


class FakeBackend:
    def __init__(self, config, state_dir, owner):
        self.config = copy.deepcopy(config)
        self.state_dir = Path(state_dir)
        self.owner = owner
        self.start_calls = 0
        self.track_calls = 0
        self.closed = False
        self.initialized = False
        self.loaded_checkpoint = None
        self.map_id = None
        self.state = "created"
        self.entities = {}
        self.first_export = True
        self.scripted = []
        self.invalidate_reasons = []

    def start(self, map_id, checkpoint=None):
        self.start_calls += 1
        if self.owner.fail_next_start:
            self.owner.fail_next_start = False
            raise RuntimeError("injected_candidate_start_failure")
        self.map_id = map_id
        self.loaded_checkpoint = copy.deepcopy(checkpoint)
        if checkpoint and checkpoint.get("availability") == "available":
            payload = json.loads((Path(checkpoint["path"]) / "state.json").read_text())
            self.entities = {(item["kind"], item["entity_id"]): item for item in payload["entities"]}
            self.initialized = bool(self.entities)
            self.state = "relocalizing"
        elif not self.config.get("calibration", {}).get("validated"):
            self.state = "awaiting_calibration"
        else:
            self.state = "initializing"

    def _default_entities(self, frame_id):
        return {
            ("keyframe", "kf-0"): {
                "op": "upsert", "kind": "keyframe", "entity_id": "kf-0",
                "data": {"position": [0, 0, 0], "T_map_camera": identity_pose(),
                         "frame_id": frame_id, "units": "arbitrary"}},
            ("sparse_point", "p-0"): {
                "op": "upsert", "kind": "sparse_point", "entity_id": "p-0",
                "data": {"xyz": [1, 2, 3], "units": "arbitrary"}},
        }

    def _apply(self, changes):
        for change in changes:
            key = (change["kind"], str(change["entity_id"]))
            if change["op"] == "delete":
                self.entities.pop(key, None)
            else:
                self.entities[key] = copy.deepcopy(change)

    def track(self, image, frame_id, timestamp):
        self.track_calls += 1
        if self.scripted:
            result = copy.deepcopy(self.scripted.pop(0))
            if isinstance(result, dict):
                self._apply(result.get("map_changes", []))
            self.initialized = True
            self.state = result.get("localization", {}).get("status", "tracking")
            return result
        created = not self.entities
        if created:
            self.entities = self._default_entities(frame_id)
        changes = list(copy.deepcopy(self.entities).values()) if self.first_export else []
        complete = self.first_export
        self.first_export = False
        self.initialized = True
        self.state = "tracking"
        return {
            "localization": {"status": "tracking", "valid": True,
                             "T_map_camera": identity_pose(), "units": "arbitrary",
                             "scale_status": "unobserved", "covariance": None},
            "geometry": {"available": True, "complete_snapshot": complete,
                         "keyframes": 1, "points": 1},
            "map_changes": changes,
            "observation_anchor": {"keyframe_id": "kf-0", "source_frame_id": frame_id}
                                  if created else None,
        }

    def checkpoint(self, target):
        if not self.initialized:
            from slam_backend import BackendNotReady
            raise BackendNotReady("synthetic_backend_not_initialized")
        target = Path(target)
        target.mkdir(parents=True, exist_ok=True)
        (target / "state.json").write_text(json.dumps({"entities": list(self.entities.values())}))
        return {"availability": "available", "path": str(target),
                "format": "synthetic_test_backend", "format_version": 1,
                "map_id": self.map_id,
                "map_changes": list(copy.deepcopy(self.entities).values()),
                "geometry": {"available": True, "complete_snapshot": True},
                "exact_saved_geometry": True}

    def status(self):
        return {"state": self.state, "status": self.state,
                "initialized": self.initialized, "map_id": self.map_id}

    def invalidate_tracking(self, reason):
        self.invalidate_reasons.append(reason)
        self.state = "lost"

    def close(self):
        self.closed = True


class VisionServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="biome-vision-service-test-")
        self.root = Path(self.tmp.name)
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.config["camera"] = {**self.config["camera"], "url": "fixture://no-network",
                                 "source_id": "synthetic-fixture", "expected_width": 32,
                                 "expected_height": 24}
        self.config["calibration"] = calibration()
        self.config["max_observations"] = 2
        self.factory = BackendFactory()
        self.services = []
        self.sequence = 0

    def tearDown(self):
        for service in reversed(self.services):
            if not service.store._closed:
                try:
                    service.close()
                finally:
                    service.store.close()
        self.tmp.cleanup()

    def make_service(self, config=None, state_dir=None):
        service = VisionService(config or self.config, state_dir or self.root / "service",
                                camera=FakeCamera(), detector=FakeDetector(),
                                backend_factory=self.factory)
        self.services.append(service)
        return service

    def frame(self, service, name="frame", *, stale=False, value=40):
        self.sequence += 1
        image = np.full((24, 32, 3), value, dtype=np.uint8)
        received = datetime.now(timezone.utc) - (timedelta(seconds=30) if stale else timedelta())
        metadata = {
            "frame_id": service.session_id + ":" + name,
            "sequence": self.sequence, "stream_epoch": "synthetic-stream",
            "width": 32, "height": 24,
            "receive_decode_time": received.isoformat(),
            "capture_time": None, "source_timestamp_quality": "receiver_only",
        }
        return SimpleNamespace(image=image, metadata=metadata, sequence=self.sequence,
                               decoded_monotonic=time.monotonic() - (30.0 if stale else 0.0))

    def process(self, service, captured):
        with service._pipeline_lock:
            return service.process_frame(captured)

    def crash(self, service):
        service._stop.set()
        service.camera.close()
        service.backend.close()
        service.store.close()

    def session_rows(self, service, database_path=None):
        path = database_path or service.state_dir / "observations/vision.sqlite3"
        with sqlite3.connect(path) as database:
            database.row_factory = sqlite3.Row
            return {row["id"]: dict(row) for row in database.execute(
                "SELECT id,map_id,state,ended_at,end_reason FROM sessions")}

    def assert_replaced_session_stopped(self, service, previous_session):
        rows = self.session_rows(service)
        self.assertEqual(rows[previous_session]["state"], "stopped")
        self.assertIsNotNone(rows[previous_session]["ended_at"])
        self.assertEqual(rows[previous_session]["end_reason"], "map_transition")
        self.assertEqual(rows[service.session_id]["state"], "running")
        self.assertIsNone(rows[service.session_id]["ended_at"])
        self.assertEqual([key for key, row in rows.items() if row["state"] == "running"],
                         [service.session_id])

    def test_snapshot_frame_and_detections_are_bound_to_same_observation(self):
        service = self.make_service()
        captured = self.frame(service, value=72)
        snapshot = self.process(service, captured)
        response = service.query("robot.vision.snapshot", {"include_frame": True})
        self.assertEqual(response["snapshot"]["snapshot_id"], snapshot["snapshot_id"])
        self.assertEqual(response["frame"]["frame_id"], snapshot["frame_id"])
        ok, expected = cv2.imencode(".jpg", captured.image, [cv2.IMWRITE_JPEG_QUALITY, 90])
        self.assertTrue(ok)
        self.assertEqual(base64.b64decode(response["frame"]["data_base64"]), expected.tobytes())
        self.assertIsInstance(snapshot["detections"], list)
        self.assertEqual(snapshot["detections"][0]["class_name"], "fixture-object")
        self.assertEqual(snapshot["frame"]["detection_metadata"]["model"], "synthetic-test-detector")
        detected = service.query("robot.vision.detections", {"snapshot_id": snapshot["snapshot_id"]})
        self.assertEqual(detected["frame_id"], snapshot["frame_id"])
        self.assertEqual(detected["detections"], snapshot["detections"])
        semantic = service.store.read_map(service.map_id, kinds=["semantic_observation"])["entities"]
        self.assertEqual(semantic[0]["data"]["snapshot_id"], snapshot["snapshot_id"])
        self.assertLess(snapshot["map_revision"], service.revision)
        self.assertEqual(service.store.get_snapshot(snapshot["snapshot_id"]), snapshot)

    def test_uncalibrated_camera_still_reports_frames_and_detections_without_fake_pose(self):
        config = copy.deepcopy(self.config)
        config["calibration"] = calibration(validated=False)
        service = self.make_service(config)
        snapshot = self.process(service, self.frame(service))
        self.assertEqual(service.backend.track_calls, 0)
        self.assertFalse(snapshot["localization"]["valid"])
        self.assertIsNone(snapshot["localization"]["T_map_camera"])
        self.assertEqual(snapshot["localization"]["status"], "awaiting_calibration")
        self.assertEqual(len(snapshot["detections"]), 1)
        self.assertEqual(service.query("robot.vision.frame")["frame"]["availability"], "available")

    def test_restart_reuses_map_but_requires_fresh_observation_in_new_session(self):
        first = self.make_service()
        snapshot = self.process(first, self.frame(first, "before"))
        map_id, session_id = first.map_id, first.session_id
        first.close()
        second = self.make_service()
        self.assertEqual(second.map_id, map_id)
        self.assertNotEqual(second.session_id, session_id)
        self.assertEqual(second.backend.state, "relocalizing")
        self.assertEqual(second.query("robot.vision.snapshot")["availability"], "unavailable")
        historical = second.query("robot.vision.snapshot", {"snapshot_id": snapshot["snapshot_id"]})["snapshot"]
        self.assertFalse(historical["freshness"]["is_current_session"])
        self.assertFalse(historical["freshness"]["is_fresh"])
        self.assertEqual(second.store.get_snapshot(snapshot["snapshot_id"]), snapshot)

    def test_authoritative_mid_session_checkpoint_removes_background_culled_geometry(self):
        service = self.make_service()
        self.process(service, self.frame(service, "before-background-cull"))
        revision_before = service.revision
        self.assertFalse(service._reconcile_geometry)
        self.assertEqual(service.store.read_map(service.map_id, kinds=["sparse_point"])["entities"][0]["entity_id"],
                         "p-0")
        service.backend.entities.pop(("sparse_point", "p-0"))
        checkpoint = service.query("robot.map.checkpoint")
        self.assertEqual(service.store.read_map(service.map_id, kinds=["sparse_point"])["entities"], [])
        previous = service.store.read_map(service.map_id, revision=revision_before,
                                           kinds=["sparse_point"])["entities"]
        self.assertEqual(previous[0]["entity_id"], "p-0")
        delta = service.store.read_deltas(service.map_id, revision_before)["changes"]
        self.assertTrue(any(item["op"] == "delete" and item["kind"] == "sparse_point"
                            and item["entity_id"] == "p-0" for item in delta))
        saved = json.loads((Path(checkpoint["backend_artifact"]["path"]) / "state.json").read_text())
        self.assertFalse(any(item["kind"] == "sparse_point" for item in saved["entities"]))

    def test_reload_older_checkpoint_removes_newer_sqlite_geometry_ghosts(self):
        first = self.make_service()
        self.process(first, self.frame(first, "checkpoint-frame"))
        first.query("robot.map.checkpoint")
        ghost = [
            {"op": "upsert", "kind": "keyframe", "entity_id": "ghost-kf",
             "data": {"position": [9, 9, 9]}},
            {"op": "upsert", "kind": "sparse_point", "entity_id": "ghost-p",
             "data": {"xyz": [9, 9, 9]}},
        ]
        first.revision = first.store.apply_map_update(first.map_id, first.revision, ghost)["revision"]
        saved_map = first.map_id
        self.crash(first)
        second = self.make_service()
        self.assertEqual(second.map_id, saved_map)
        self.process(second, self.frame(second, "after-reload"))
        entities = second.store.read_map(second.map_id, kinds=["keyframe", "sparse_point"])["entities"]
        identities = {(item["kind"], item["entity_id"]) for item in entities}
        self.assertEqual(identities, {("keyframe", "kf-0"), ("sparse_point", "p-0")})
        self.assertFalse(second._reconcile_geometry)

    def test_restart_uses_backend_checkpoint_even_after_metadata_only_checkpoint(self):
        first = self.make_service()
        self.process(first, self.frame(first))
        usable = first.query("robot.map.checkpoint")
        first.store.checkpoint(first.map_id, metadata={"purpose": "metadata-only-test"})
        self.crash(first)
        second = self.make_service()
        self.assertIsNotNone(second.backend.loaded_checkpoint)
        self.assertEqual(second.backend.loaded_checkpoint["path"], usable["backend_artifact"]["path"])
        self.assertEqual(second.backend.state, "relocalizing")

    def test_first_valid_calibration_works_without_preexisting_backend_map(self):
        config = copy.deepcopy(self.config)
        config["calibration"] = calibration(validated=False)
        service = self.make_service(config)
        old_map, old_session = service.map_id, service.session_id
        updated = calibration(identifier=None, validated=True)
        result = service.query("robot.vision.calibration", {"action": "set", "calibration": updated})
        self.assertTrue(result["previous_map_preserved"])
        self.assertNotEqual(service.map_id, old_map)
        self.assertNotEqual(service.session_id, old_session)
        self.assertEqual(service.store.read_map(old_map)["map"]["map_id"], old_map)
        snapshot = self.process(service, self.frame(service, "calibrated"))
        self.assertTrue(snapshot["localization"]["valid"])
        self.assertEqual(snapshot["calibration_id"], result["calibration_id"])

    def test_calibration_change_keeps_old_map_and_starts_new_session(self):
        service = self.make_service()
        old_snapshot = self.process(service, self.frame(service))
        old_map, old_session = service.map_id, service.session_id
        changed = calibration("synthetic-calibration-v2")
        changed["fx"] = 41.0
        result = service.query("robot.vision.calibration", {"action": "set", "calibration": changed})
        self.assertEqual(result["previous_map_id"], old_map)
        self.assertNotEqual(service.map_id, old_map)
        self.assertNotEqual(service.session_id, old_session)
        self.assertEqual(service.query("robot.vision.snapshot")["availability"], "unavailable")
        self.assertEqual(service.store.get_snapshot(old_snapshot["snapshot_id"]), old_snapshot)
        self.assertEqual(service.store.read_map(old_map)["map"]["metadata"]["calibration_id"],
                         "synthetic-calibration-v1")
        self.assertEqual(service.calibration["calibration_id"], "synthetic-calibration-v2")

    def test_freshness_exposes_assessment_expiry_and_excludes_unmeasured_delivery_latency(self):
        service = self.make_service()
        snapshot = self.process(service, self.frame(service, "freshness-provenance"))
        initial = service.query("robot.vision.snapshot", {"snapshot_id": snapshot["snapshot_id"]})["snapshot"]
        freshness = initial["freshness"]
        assessed = datetime.fromisoformat(freshness["assessed_at_mac_utc"])
        expires = datetime.fromisoformat(freshness["valid_until_mac_utc"])
        self.assertIsNotNone(assessed.tzinfo)
        self.assertIsNotNone(expires.tzinfo)
        self.assertFalse(freshness["delivery_latency_included"])
        self.assertTrue(freshness["is_fresh"])
        self.assertGreater(expires, assessed)
        self.assertLessEqual((expires - assessed).total_seconds(),
                             self.config["freshness_threshold_s"] + 0.1)
        future_utc = datetime.now(timezone.utc) + timedelta(seconds=10)
        future_monotonic = time.monotonic() + 10

        class FutureDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return future_utc.astimezone(tz) if tz else future_utc.replace(tzinfo=None)

        with patch("vision_service.datetime", FutureDateTime), \
                patch("vision_service.time.monotonic", return_value=future_monotonic):
            delayed = service.query("robot.vision.snapshot", {"snapshot_id": snapshot["snapshot_id"]})["snapshot"]
        self.assertEqual(delayed["snapshot_id"], snapshot["snapshot_id"])
        self.assertFalse(delayed["freshness"]["is_fresh"])
        self.assertGreater(delayed["freshness"]["receive_decode_age_s"], 9)
        self.assertIsNone(delayed["freshness"]["capture_age_s"])
        self.assertLessEqual(
            datetime.fromisoformat(delayed["freshness"]["valid_until_mac_utc"]),
            datetime.fromisoformat(delayed["freshness"]["assessed_at_mac_utc"]))

    def test_map_create_preserves_authoritative_units_calibration_and_estimator_metadata(self):
        service = self.make_service()
        active = service.map_id
        created = service.query("robot.map.create", {"metadata": {
            "name": "descriptive user label", "units": "m", "scale_status": "metric",
            "calibration_id": "nonexistent-calibration", "kind": "claimed_dense_occupancy",
            "origin": "claimed_absolute_origin"}})
        metadata = created["metadata"]
        self.assertEqual(metadata["name"], "descriptive user label")
        self.assertEqual(metadata["units"], "arbitrary")
        self.assertEqual(metadata["scale_status"], "unobserved")
        self.assertEqual(metadata["calibration_id"], service.calibration["calibration_id"])
        self.assertEqual(metadata["kind"], "monocular_sparse_map")
        self.assertEqual(metadata["origin"], "first_localized_camera")
        self.assertEqual(service.map_id, active)

    def test_stale_observation_is_marked_stale_without_fabricated_capture_age(self):
        service = self.make_service()
        snapshot = self.process(service, self.frame(service, stale=True))
        result = service.query("robot.vision.snapshot", {"snapshot_id": snapshot["snapshot_id"]})["snapshot"]
        self.assertFalse(result["freshness"]["is_fresh"])
        self.assertTrue(result["freshness"]["is_current_session"])
        self.assertGreater(result["freshness"]["receive_decode_age_s"], 20)
        self.assertIsNone(result["freshness"]["capture_age_s"])

    def test_lost_tracking_clears_an_invalid_stale_pose(self):
        service = self.make_service()
        self.process(service, self.frame(service, "tracked"))
        service.backend.scripted.append({
            "localization": {"status": "lost", "valid": False,
                             "T_map_camera": identity_pose(), "units": "arbitrary"},
            "geometry": {"available": False, "reason": "lost"}, "map_changes": []})
        lost = self.process(service, self.frame(service, "lost"))
        self.assertFalse(lost["localization"]["valid"])
        self.assertIsNone(lost["localization"]["T_map_camera"])
        self.assertEqual(lost["localization"]["status"], "lost")

    def test_encoding_failure_cannot_commit_dangling_semantic_observation(self):
        service = self.make_service()
        with patch("cv2.imencode", return_value=(False, None)):
            with self.assertRaisesRegex(RuntimeError, "encoding"):
                self.process(service, self.frame(service, "encode-failure"))
        self.assertEqual(service.store.read_map(service.map_id, kinds=["semantic_observation"])["entities"], [])
        self.assertEqual(service.store.latest_snapshot(session_id=service.session_id)["availability"],
                         "unavailable")

    def test_publish_failure_cannot_commit_dangling_semantic_observation(self):
        service = self.make_service()
        with patch.object(service.store, "publish_observation", side_effect=RuntimeError("injected_publish_failure")):
            with self.assertRaisesRegex(RuntimeError, "injected_publish"):
                self.process(service, self.frame(service, "publish-failure"))
        self.assertEqual(service.store.read_map(service.map_id, kinds=["semantic_observation"])["entities"], [])
        self.assertEqual(service.store.latest_snapshot(session_id=service.session_id)["availability"],
                         "unavailable")

    def test_pending_map_updates_recover_after_transient_failure_without_duplicate_entity_loop(self):
        service = self.make_service()
        original = service.store.apply_map_update
        with patch.object(service.store, "apply_map_update", side_effect=RuntimeError("injected_database_busy")):
            with self.assertRaisesRegex(RuntimeError, "injected_database_busy"):
                self.process(service, self.frame(service, "failed-commit"))
        service.backend.scripted.append({
            "localization": {"status": "tracking", "valid": True,
                             "T_map_camera": identity_pose(), "units": "arbitrary"},
            "geometry": {"available": True, "complete_snapshot": False},
            "map_changes": [{"op": "upsert", "kind": "sparse_point", "entity_id": "p-0",
                             "data": {"xyz": [4, 5, 6], "units": "arbitrary"}}],
        })
        snapshot = self.process(service, self.frame(service, "recovered-commit"))
        self.assertTrue(snapshot["localization"]["valid"])
        points = service.store.read_map(service.map_id, kinds=["sparse_point"])["entities"]
        self.assertEqual(points[0]["data"]["xyz"], [4, 5, 6])
        self.assertEqual(service._pending_changes, [])

    def test_corrupt_startup_checkpoint_preserves_perception_and_explicit_reopen_recovers(self):
        first = self.make_service()
        self.process(first, self.frame(first, "saved-before-corruption"))
        checkpoint = first.query("robot.map.checkpoint")
        artifact = Path(checkpoint["backend_artifact"]["path"]) / "state.json"
        original = artifact.read_bytes()
        artifact.write_text("{invalid-test-json")
        map_id, old_session = first.map_id, first.session_id
        expected_calibration = copy.deepcopy(first.calibration)
        self.crash(first)
        attempts_before = len(self.factory.instances)
        second = self.make_service()
        attempts = self.factory.instances[attempts_before:]
        self.assertEqual(second.map_id, map_id)
        self.assertNotEqual(second.session_id, old_session)
        self.assertEqual(second.calibration, expected_calibration)
        self.assertTrue(any(item.start_calls and item.closed for item in attempts))
        snapshot = self.process(second, self.frame(second, "during-map-recovery-error"))
        self.assertFalse(snapshot["localization"]["valid"])
        self.assertIsNone(snapshot["localization"]["T_map_camera"])
        self.assertEqual(second.backend.track_calls, 0)
        self.assertEqual(len(snapshot["detections"]), 1)
        self.assertEqual(second.query("robot.vision.frame")["frame"]["availability"], "available")
        points = second.store.read_map(map_id, kinds=["sparse_point"])["entities"]
        self.assertEqual([item["entity_id"] for item in points], ["p-0"])
        degraded_session = second.session_id
        artifact.write_bytes(original)
        reopened = second.query("robot.map.open", {"map_id": map_id})
        self.assertEqual(reopened["map_id"], map_id)
        self.assertNotEqual(second.session_id, degraded_session)
        self.assertEqual(second.query("robot.vision.snapshot")["availability"], "unavailable")
        recovered = self.process(second, self.frame(second, "relocalized-after-repair"))
        self.assertTrue(recovered["localization"]["valid"])
        self.assertEqual(second.backend.track_calls, 1)

    def test_stored_geometry_without_backend_checkpoint_keeps_perception_but_refuses_localization(self):
        first = self.make_service()
        first.revision = first.store.apply_map_update(
            first.map_id, 0, [{"op": "upsert", "kind": "sparse_point", "entity_id": "saved-only",
                               "data": {"xyz": [8, 8, 8]}}])["revision"]
        map_id = first.map_id
        self.crash(first)
        second = self.make_service()
        self.assertEqual(second.map_id, map_id)
        snapshot = self.process(second, self.frame(second, "no-backend-checkpoint"))
        self.assertEqual(second.backend.start_calls, 0)
        self.assertEqual(second.backend.track_calls, 0)
        self.assertFalse(snapshot["localization"]["valid"])
        self.assertIsNone(snapshot["localization"]["T_map_camera"])
        self.assertEqual(len(snapshot["detections"]), 1)
        self.assertEqual(second.store.read_map(map_id, kinds=["sparse_point"])["entities"][0]["entity_id"],
                         "saved-only")

    def test_failed_map_open_reloads_previous_map_in_fresh_session(self):
        service = self.make_service()
        self.process(service, self.frame(service))
        old_map, old_session, old_backend = service.map_id, service.session_id, service.backend
        target = service.query("robot.map.create")["map_id"]
        self.factory.fail_next_start = True
        with self.assertRaisesRegex(RuntimeError, "candidate_start"):
            service.query("robot.map.open", {"map_id": target})
        self.assertEqual(service.map_id, old_map)
        self.assertNotEqual(service.session_id, old_session)
        self.assert_replaced_session_stopped(service, old_session)
        self.assertIsNot(service.backend, old_backend)
        self.assertTrue(old_backend.closed)
        self.assertIsNotNone(service.backend.loaded_checkpoint)
        self.assertEqual(service.query("robot.vision.snapshot")["availability"], "unavailable")
        self.assertEqual(json.loads((service.state_dir / "runtime.json").read_text())["map_id"], old_map)

    def test_failed_calibration_switch_restores_previous_map_without_stale_pose(self):
        service = self.make_service()
        self.process(service, self.frame(service))
        old_map, old_session, old_backend = service.map_id, service.session_id, service.backend
        old_calibration = copy.deepcopy(service.calibration)
        self.factory.fail_next_start = True
        with self.assertRaisesRegex(RuntimeError, "candidate_start"):
            service.query("robot.vision.calibration",
                          {"action": "set", "calibration": calibration("failed-candidate-calibration")})
        self.assertEqual(service.map_id, old_map)
        self.assertNotEqual(service.session_id, old_session)
        self.assert_replaced_session_stopped(service, old_session)
        self.assertEqual(service.calibration, old_calibration)
        self.assertIsNot(service.backend, old_backend)
        self.assertTrue(old_backend.closed)
        self.assertIsNotNone(service.backend.loaded_checkpoint)
        self.assertEqual(service.query("robot.vision.snapshot")["availability"], "unavailable")


    def test_shutdown_checkpoint_contains_stopped_session(self):
        service = self.make_service()
        self.process(service, self.frame(service, "before-shutdown"))
        session_id = service.session_id
        checkpoints = []
        original_checkpoint = service.store.checkpoint

        def capture_checkpoint(*args, **kwargs):
            saved = original_checkpoint(*args, **kwargs)
            checkpoints.append(saved)
            return saved

        with patch.object(service.store, "checkpoint", side_effect=capture_checkpoint):
            service.close()
        self.assertTrue(service.camera.closed)
        self.assertTrue(service.store._closed)
        self.assertEqual(len(checkpoints), 1)
        current = self.session_rows(service)[session_id]
        saved = self.session_rows(service, checkpoints[0]["database_path"])[session_id]
        self.assertEqual(current["state"], "stopped")
        self.assertIsNotNone(current["ended_at"])
        self.assertEqual(current["end_reason"], "service_shutdown")
        self.assertEqual(saved, current)

    def test_successful_map_switch_finalizes_replaced_session(self):
        service = self.make_service()
        self.process(service, self.frame(service, "before-switch"))
        previous_session, previous_backend = service.session_id, service.backend
        target = service.query("robot.map.create")["map_id"]
        service.query("robot.map.open", {"map_id": target})
        self.assertEqual(service.map_id, target)
        self.assertTrue(previous_backend.closed)
        self.assert_replaced_session_stopped(service, previous_session)
        self.assertEqual(sum(not item.closed for item in self.factory.instances), 1)

    def test_failed_activation_stops_candidate_before_rollback(self):
        import vision_service

        service = self.make_service()
        self.process(service, self.frame(service, "before-failed-runtime-write"))
        prior_map, prior_session = service.map_id, service.session_id
        target = service.query("robot.map.create")["map_id"]
        original_write = vision_service.atomic_json
        original_prepare = service._prepare_backend
        rollback_checked = []

        def reject_candidate(path, value):
            if value.get("map_id") == target:
                raise OSError("injected_runtime_selection_failure")
            return original_write(path, value)

        def inspect_before_rollback(map_id, *args, **kwargs):
            if map_id == prior_map:
                candidates = [row for row in self.session_rows(service).values()
                              if row["map_id"] == target]
                self.assertEqual(len(candidates), 1)
                self.assertEqual(candidates[0]["state"], "stopped")
                self.assertIsNotNone(candidates[0]["ended_at"])
                self.assertEqual(candidates[0]["end_reason"], "activation_failed")
                rollback_checked.append(True)
            return original_prepare(map_id, *args, **kwargs)

        with patch("vision_service.atomic_json", side_effect=reject_candidate), \
                patch.object(service, "_prepare_backend", side_effect=inspect_before_rollback):
            with self.assertRaisesRegex(OSError, "runtime_selection"):
                service.query("robot.map.open", {"map_id": target})
        self.assertEqual(rollback_checked, [True])
        self.assertEqual(service.map_id, prior_map)
        self.assertNotEqual(service.session_id, prior_session)
        self.assert_replaced_session_stopped(service, prior_session)
        self.assertEqual(sum(not item.closed for item in self.factory.instances), 1)

    def test_backend_close_failure_still_closes_store_and_ends_session(self):
        service = self.make_service()
        session_id = service.session_id
        with patch.object(service.backend, "close", side_effect=RuntimeError("injected_backend_close_failure")):
            with self.assertRaisesRegex(RuntimeError, "backend_close"):
                service.close()
        self.assertTrue(service.camera.closed)
        self.assertTrue(service.store._closed)
        row = self.session_rows(service)[session_id]
        self.assertEqual(row["state"], "stopped")
        self.assertIsNotNone(row["ended_at"])
        self.assertEqual(row["end_reason"], "service_shutdown")


if __name__ == "__main__":
    unittest.main()
