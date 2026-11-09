"""Behavioral tests for persistence, identity, retention and map consistency."""
import base64
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vision_store import VisionStore, RevisionConflict


class VisionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="biome-vision-store-test-")
        self.root = Path(self.tmp.name)
        self.store = VisionStore(self.root / "state", max_observations=3)
        self.map_id = self.store.create_map({"name": "test environment"}, map_id="map-test")["map_id"]
        self.session_id = self.store.start_session(self.map_id, camera={"transport": "RTP"})["session_id"]

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def publish(self, name, *, session_id=None, image=None, **kwargs):
        return self.store.publish_observation(
            session_id or self.session_id,
            {"frame_id": name, "test_value": name, "width_px": 16, "height_px": 12},
            [{"class_name": "test-fixture", "confidence": 0.8, "bbox_xyxy_px": [1, 2, 3, 4]}],
            image_bytes=image if image is not None else name.encode("ascii"), **kwargs)

    def point(self, entity_id, xyz, **extras):
        return {"op": "upsert", "kind": "sparse_point", "entity_id": entity_id,
                "data": {"xyz": xyz, **extras}}

    def test_snapshot_and_exact_frame_are_coherent_json(self):
        snapshot = self.publish("frame-one")
        result = self.store.get_snapshot_with_frame(snapshot["snapshot_id"])
        self.assertEqual(result["snapshot"]["frame_id"], result["frame"]["frame_id"])
        self.assertEqual(result["snapshot"], snapshot)
        self.assertEqual(base64.b64decode(result["frame"]["data_base64"]), b"frame-one")
        self.assertEqual(result["frame"]["sha256"], hashlib.sha256(b"frame-one").hexdigest())
        self.assertIsNone(snapshot["frame"]["captured_at"])
        self.assertIsNone(snapshot["frame"]["received_at"])
        self.assertEqual(snapshot["frame"]["capture_timestamp_source"], "not_provided")
        json.dumps(result, allow_nan=False)

    def test_unknown_estimates_and_scale_stay_unknown(self):
        snapshot = self.publish("unknown")
        self.assertEqual(snapshot["localization"]["availability"], "unavailable")
        self.assertEqual(snapshot["localization"]["units"], "arbitrary")
        self.assertEqual(snapshot["localization"]["scale_status"], "unanchored")
        self.assertIsNone(snapshot["localization"]["covariance"])
        self.assertEqual(snapshot["spatial_evidence"]["availability"], "unavailable")
        self.assertEqual(snapshot["tracks_availability"], "unavailable")
        self.assertNotIn("velocity", snapshot["localization"])

    def test_invalid_world_pose_rolls_back_without_blob_or_sequence_consumption(self):
        before = self.store.status()
        with self.assertRaisesRegex(ValueError, "map_revision"):
            self.publish("invalid", localization={"map_from_camera_transform": [[1, 0], [0, 1]]})
        self.assertEqual(self.store.status()["counts"]["frames"], before["counts"]["frames"])
        self.assertEqual(self.store.status()["image_blob_count"], 0)
        self.assertEqual(self.publish("valid")["frame"]["sequence"], 0)
        with self.assertRaises(ValueError):
            self.store.publish_observation(self.session_id, {"frame_id": "nan"},
                                           [{"confidence": float("nan")}], image_bytes=b"nan")
        self.assertEqual(self.store.status()["counts"]["frames"], 1)
        self.assertEqual(self.store.status()["image_blob_count"], 1)

    def test_frame_id_cannot_be_overwritten(self):
        first = self.publish("immutable", image=b"original")
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.publish("immutable", image=b"different")
        result = self.store.get_snapshot_with_frame(first["snapshot_id"])
        self.assertEqual(base64.b64decode(result["frame"]["data_base64"]), b"original")
        self.assertEqual(self.store.status()["image_blob_count"], 1)

    def test_image_file_input_is_copied_and_remains_caller_owned(self):
        path = self.root / "camera-frame.bin"
        path.write_bytes(b"captured-original")
        snapshot = self.store.publish_observation(
            self.session_id, {"frame_id": "path-frame"}, [], image_path=str(path))
        path.write_bytes(b"later-camera-frame")
        result = self.store.get_snapshot_with_frame(snapshot["snapshot_id"])
        self.assertEqual(base64.b64decode(result["frame"]["data_base64"]), b"captured-original")
        self.assertEqual(path.read_bytes(), b"later-camera-frame")

    def test_global_rolling_retention_across_sessions(self):
        first = self.publish("old-session")
        session2 = self.store.start_session(self.map_id)["session_id"]
        for index in range(5):
            self.publish("new-%s" % index, session_id=session2)
        self.assertEqual(self.store.status()["counts"]["frames"], 3)
        self.assertEqual(self.store.status()["image_blob_count"], 3)
        self.assertEqual(self.store.get_frame(first["frame_id"])["availability"], "unavailable")
        self.assertEqual(self.store.get_snapshot(first["snapshot_id"])["availability"], "unavailable")
        self.assertEqual(self.store.latest_snapshot()["frame_id"], "new-4")

    def test_keyframe_observations_survive_without_archiving_old_images(self):
        first = self.publish("keyframe-image", keyframe_id="kf-7")
        for index in range(5):
            self.publish("rolling-%s" % index)
        self.assertEqual(self.store.get_snapshot(first["snapshot_id"])["availability"], "available")
        self.assertEqual(self.store.get_frame(first["frame_id"])["availability"], "unavailable")
        observations = self.store.read_observations(self.map_id, keyframe_id="kf-7")["observations"]
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["frame_id"], "keyframe-image")
        self.assertEqual(self.store.status()["image_blob_count"], 3)

    def test_pin_reasons_are_independent_and_idempotent(self):
        pinned = self.publish("pinned")
        self.store.pin_frame(pinned["frame_id"], "reader-a")
        self.store.pin_frame(pinned["frame_id"], "reader-a")
        self.store.pin_frame(pinned["frame_id"], "reader-b")
        for index in range(5):
            self.publish("pin-roll-%s" % index)
        self.assertEqual(self.store.status()["counts"]["frame_pins"], 2)
        self.assertEqual(self.store.get_frame("pinned")["availability"], "available")
        self.assertEqual(self.store.unpin_frame("pinned", "reader-a")["availability"], "available")
        self.assertEqual(self.store.unpin_frame("pinned", "reader-b")["availability"], "unavailable")
        self.assertEqual(self.store.get_frame("pinned")["frame_id"], "pinned")

    def test_missing_and_unsupplied_images_do_not_fall_back_to_latest(self):
        missing_image = self.store.publish_observation(
            self.session_id, {"frame_id": "no-image"}, [])
        latest = self.publish("latest")
        self.assertEqual(self.store.get_frame("never-seen")["availability"], "unavailable")
        result = self.store.get_snapshot_with_frame(missing_image["snapshot_id"])
        self.assertEqual(result["snapshot"]["frame_id"], "no-image")
        self.assertEqual(result["frame"]["availability"], "unavailable")
        self.assertNotIn("data_base64", result["frame"])
        self.assertEqual(self.store.latest_snapshot()["frame_id"], latest["frame_id"])

    def test_map_revisions_region_queries_and_tombstones(self):
        first = self.store.apply_map_update(self.map_id, 0, [
            self.point("near", [1, 2, 0]), self.point("far", [9, 9, 3])])
        self.assertEqual(first["revision"], 1)
        near = self.store.read_map(self.map_id, bounds=[0, 0, 2, 3])["entities"]
        self.assertEqual([row["entity_id"] for row in near], ["near"])
        self.store.apply_map_update(self.map_id, 1, [
            self.point("near", [5, 6, 0]),
            {"op": "delete", "kind": "sparse_point", "entity_id": "far"}], reason="loop_closure")
        before = self.store.read_map(self.map_id, revision=1)["entities"]
        after = self.store.read_map(self.map_id, revision=2)["entities"]
        self.assertEqual(len(before), 2)
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]["data"]["xyz"], [5, 6, 0])
        self.assertEqual(before[1]["data"]["xyz"], [1, 2, 0])
        self.assertEqual(self.store.read_map(self.map_id, bounds=[0, 0, 2, 3])["entities"], [])

    def test_conflict_and_bad_update_do_not_advance_revision(self):
        self.store.apply_map_update(self.map_id, 0, [self.point("a", [1, 2, 3])])
        with self.assertRaises(RevisionConflict):
            self.store.apply_map_update(self.map_id, 0, [self.point("b", [4, 5, 6])])
        with self.assertRaises(ValueError):
            self.store.apply_map_update(self.map_id, 1, [self.point("bad", [1, float("nan"), 3])])
        self.assertEqual(self.store.read_map(self.map_id)["map"]["revision"], 1)
        self.assertEqual(len(self.store.read_map(self.map_id)["entities"]), 1)

    def test_map_queries_cannot_mix_maps(self):
        other = self.store.create_map(map_id="other")["map_id"]
        self.store.apply_map_update(self.map_id, 0, [self.point("same-id", [1, 2, 3])])
        self.store.apply_map_update(other, 0, [self.point("same-id", [8, 9, 0])])
        self.assertEqual(self.store.read_map(self.map_id)["entities"][0]["data"]["xyz"], [1, 2, 3])
        self.assertEqual(self.store.read_map(other)["entities"][0]["data"]["xyz"], [8, 9, 0])

    def test_pose_revision_is_preserved_and_units_are_checked(self):
        self.store.apply_map_update(self.map_id, 0, [self.point("a", [1, 2, 3])])
        pose = {"state": "tracking", "map_revision": 0,
                "map_from_camera_transform": [[1, 0, 0, 0], [0, 1, 0, 0],
                                              [0, 0, 1, 0], [0, 0, 0, 1]]}
        snapshot = self.publish("old-revision", localization=pose)
        self.assertEqual(snapshot["map_revision"], 0)
        self.assertEqual(snapshot["localization"]["map_revision"], 0)
        with self.assertRaises(ValueError):
            self.publish("wrong-units", localization={**pose, "units": "m"})
        with self.assertRaises(ValueError):
            self.publish("wrong-map", localization={**pose, "map_id": "other"})
        with self.assertRaises(KeyError):
            self.publish("future-revision", localization={**pose, "map_revision": 99})

    def test_map_and_delta_pages_pin_the_revision(self):
        self.store.apply_map_update(self.map_id, 0, [
            self.point("a", [1, 0, 0]), self.point("b", [2, 0, 0]), self.point("c", [3, 0, 0])])
        first = self.store.read_map(self.map_id, limit=1)
        first_delta = self.store.read_deltas(self.map_id, 0, limit=2)
        self.store.apply_map_update(self.map_id, 1, [self.point("d", [4, 0, 0])])
        ids = [entity["entity_id"] for entity in first["entities"]]
        page = first
        while page["next_cursor"]:
            page = self.store.read_map(self.map_id, cursor=page["next_cursor"], limit=1)
            self.assertEqual(page["map"]["revision"], 1)
            ids.extend(entity["entity_id"] for entity in page["entities"])
        self.assertEqual(ids, ["a", "b", "c"])
        changes = first_delta["changes"][:]
        page = first_delta
        while page["next_cursor"]:
            page = self.store.read_deltas(self.map_id, 0, cursor=page["next_cursor"], limit=2)
            self.assertEqual(page["target_revision"], 1)
            changes.extend(page["changes"])
        self.assertEqual([item["entity_id"] for item in changes], ["a", "b", "c"])
        with self.assertRaises(ValueError):
            self.store.read_map(self.map_id, kinds=["different"], cursor=first["next_cursor"])

    def test_metadata_only_revisions_are_visible_in_deltas(self):
        self.store.apply_map_update(self.map_id, 0, [], metadata={"name": "renamed"})
        result = self.store.read_deltas(self.map_id, 0, limit=1)
        self.assertEqual(result["changes"], [])
        self.assertEqual(result["revisions"][0]["metadata"]["name"], "renamed")
        self.assertIsNone(result["next_cursor"])

    def test_calibration_provenance_is_immutable_and_survives_restart(self):
        provenance = {"height_m": 0.0635, "pitch_down_deg": 0.0,
                      "mounting_source": "user_reported_provisional",
                      "intrinsics": None, "distortion": None}
        calibration = self.store.save_calibration(provenance, calibration_id="calibration-v1")
        provenance["height_m"] = 9
        self.assertEqual(self.store.get_calibration("calibration-v1")["calibration"]["height_m"], 0.0635)
        with self.assertRaises(ValueError):
            self.store.save_calibration(provenance, calibration_id="calibration-v1")
        self.publish("before-restart")
        self.store.apply_map_update(self.map_id, 0, [self.point("saved", [1, 2, 3])])
        self.store.close()
        self.store = VisionStore(self.root / "state", max_observations=3)
        self.assertEqual(self.store.get_calibration("calibration-v1"), calibration)
        self.assertEqual(self.store.read_map(self.map_id)["entities"][0]["entity_id"], "saved")
        self.assertEqual(self.store.latest_snapshot()["frame_id"], "before-restart")
        new = self.store.start_session(self.map_id)["session_id"]
        self.assertNotEqual(new, self.session_id)
        self.assertEqual(self.store.latest_snapshot(session_id=new)["availability"], "unavailable")
        self.assertEqual(self.store.status()["runtime_liveness"], "not_observed_by_store")

    def test_checkpoint_has_real_database_and_copied_artifact_without_false_qualification(self):
        self.store.apply_map_update(self.map_id, 0, [self.point("saved", [1, 2, 3])])
        artifact = self.root / "backend"
        artifact.mkdir()
        (artifact / "test-state.bin").write_bytes(b"backend-test-fixture")
        checkpoint = self.store.checkpoint(self.map_id, artifact,
                                           {"backend_format": "test-fixture-v1", "map_revision": 1})
        json.dumps(checkpoint, allow_nan=False)
        copied = Path(checkpoint["backend_artifact"]["path"]) / "test-state.bin"
        (artifact / "test-state.bin").write_bytes(b"changed-source")
        self.assertEqual(copied.read_bytes(), b"backend-test-fixture")
        self.assertFalse(checkpoint["backend_artifact"]["qualified_for_relocalization"])
        self.assertEqual(checkpoint["relocalization_qualification"], "not_tested_by_store")
        path = Path(checkpoint["database_path"])
        self.assertTrue(path.is_file())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), checkpoint["database_sha256"])
        with sqlite3.connect(path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM entity_versions").fetchone()[0], 1)

    def test_metadata_only_checkpoint_does_not_hide_reusable_backend(self):
        self.assertEqual(self.store.latest_backend_checkpoint(self.map_id)["availability"], "unavailable")
        artifact = self.root / "backend-state-fixture.bin"
        artifact.write_bytes(b"unit-test-backend-state")
        usable = self.store.checkpoint(self.map_id, artifact, {"format": "unit-test-fixture"})
        later = self.store.checkpoint(self.map_id, metadata={"reason": "metadata_only"})
        self.assertEqual(self.store.latest_checkpoint(self.map_id)["checkpoint_id"], later["checkpoint_id"])
        self.assertEqual(self.store.latest_backend_checkpoint(self.map_id)["checkpoint_id"], usable["checkpoint_id"])
        self.assertEqual(self.store.get_checkpoint(usable["checkpoint_id"])["backend_artifact"]["availability"],
                         "available")

    def test_checkpoint_without_backend_remains_unqualified_and_failure_is_clean(self):
        checkpoint = self.store.checkpoint(self.map_id)
        self.assertEqual(checkpoint["backend_artifact"]["availability"], "unavailable")
        before = self.store.status()["counts"]["checkpoints"]
        with self.assertRaises(ValueError):
            self.store.checkpoint(self.map_id, self.root / "missing")
        self.assertEqual(self.store.status()["counts"]["checkpoints"], before)
        with self.assertRaises(ValueError):
            self.store.checkpoint(self.map_id, self.root / "state")
        self.assertEqual(self.store.status()["counts"]["checkpoints"], before)

    def test_atomic_latest_frame_during_concurrent_publish_and_eviction(self):
        self.store.close()
        self.store = VisionStore(self.root / "state", max_observations=1)
        self.publish("seed")
        failures = []
        def writer():
            try:
                for index in range(40):
                    self.publish("concurrent-%s" % index)
            except BaseException as error:
                failures.append(error)
        thread = threading.Thread(target=writer)
        thread.start()
        for _ in range(80):
            pair = self.store.get_snapshot_with_frame(session_id=self.session_id)
            self.assertEqual(pair["frame"]["availability"], "available")
            self.assertEqual(pair["snapshot"]["frame_id"], pair["frame"]["frame_id"])
            self.assertEqual(base64.b64decode(pair["frame"]["data_base64"]).decode("ascii"),
                             pair["snapshot"]["frame"]["test_value"])
        thread.join()
        self.assertEqual(failures, [])
        self.assertEqual(self.store.status()["image_blob_count"], 1)

    def test_corrupt_frame_is_unavailable_instead_of_serving_bad_bytes(self):
        snapshot = self.publish("integrity")
        blob = next((self.root / "state" / "frame_blobs").glob("*.blob"))
        blob.write_bytes(b"corrupt")
        result = self.store.get_snapshot_with_frame(snapshot["snapshot_id"])
        self.assertEqual(result["frame"]["availability"], "unavailable")
        self.assertEqual(result["frame"]["reason"], "image_integrity_error")
        self.assertNotIn("data_base64", result["frame"])

    def test_retention_promotion_preserves_immutable_snapshot(self):
        snapshot = self.publish("promoted")
        retained = self.store.retain_observation(snapshot["snapshot_id"], "semantic-map-evidence")
        self.assertEqual(retained["retention_reasons"], ["semantic-map-evidence"])
        self.assertEqual(self.store.get_snapshot(snapshot["snapshot_id"]), snapshot)
        for index in range(5):
            self.publish("promotion-roll-%s" % index)
        self.assertEqual(self.store.get_snapshot(snapshot["snapshot_id"]), snapshot)
        self.assertEqual(self.store.get_frame(snapshot["frame_id"])["availability"], "unavailable")
        evidence = self.store.read_observations(self.map_id)["observations"]
        self.assertTrue(any(item["frame_id"] == "promoted" for item in evidence))

    def test_stop_session_prevents_new_observation_but_preserves_map_and_frames(self):
        snapshot = self.publish("stopped")
        self.store.stop_session(self.session_id, "test")
        with self.assertRaises(ValueError):
            self.publish("after-stop")
        self.assertEqual(self.store.get_snapshot(snapshot["snapshot_id"])["availability"], "available")
        self.assertEqual(self.store.read_map(self.map_id)["map"]["map_id"], self.map_id)


if __name__ == "__main__":
    unittest.main()
