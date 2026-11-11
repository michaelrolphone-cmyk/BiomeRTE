"""Metric integration fixtures. No vehicle, camera network or physical claim."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from metric_sessions import MetricCalibration
from vision_store import VisionStore


def pose(x=0.0, y=0.0, z=0.0):
    value = np.eye(4)
    value[:3, 3] = [x, y, z]
    return value.tolist()


class MetricSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="biome-metric-integration-")
        self.root = Path(self.tmp.name)
        self.store = VisionStore(self.root, max_observations=2)
        self.calibration = {
            "calibration_id": "synthetic-calibration", "validated": True,
            "provenance": "synthetic_metric_integration_fixture",
            "width": 1280, "height": 720, "fx": 600.0, "fy": 600.0,
            "cx": 640.0, "cy": 360.0, "distortion": [0.0]*5,
        }
        self.store.save_calibration(self.calibration)
        self.map_id = self.store.create_map(
            {"calibration_id": self.calibration["calibration_id"]})["map_id"]
        self.obs_session = self.store.start_session(
            self.map_id, calibration=self.calibration["calibration_id"])["session_id"]
        self.manager = MetricCalibration(self.store)
        self.reference = {
            "inner_corners": [9, 6], "square_size_m": 0.02, "size_verified": True,
            "measurement_source": "synthetic_fixture_known_dimension",
            "stationary": True, "reference_board_id": "synthetic-board",
        }
        self.snapshots = []
        self.positions = {}
        ok, encoded = cv2.imencode(".jpg", np.zeros((720,1280,3), dtype=np.uint8))
        self.assertTrue(ok)
        self.jpeg = encoded.tobytes()

    def tearDown(self):
        self.manager.close()
        if not self.store._closed:
            self.store.close()
        self.tmp.cleanup()

    def frame(self, x, *, exact=True, map_id=None):
        index = len(self.snapshots)
        frame_id, keyframe_id = "frame-" + str(index), "kf-" + str(index)
        map_id = map_id or self.map_id
        revision = self.store.read_map(map_id, kinds=[], limit=1)["map"]["revision"]
        if exact:
            result = self.store.apply_map_update(map_id, revision, [{
                "op": "upsert", "kind": "keyframe", "entity_id": keyframe_id,
                "data": {"T_map_camera": pose(x), "position": [x,0,0],
                         "source_frame_id": frame_id, "units": "arbitrary",
                         "covariance": None},
            }])
            revision = result["revision"]
        stamp = datetime.now(timezone.utc).isoformat()
        snapshot = self.store.publish_observation(
            self.obs_session, {
                "frame_id": frame_id, "width": 1280, "height": 720,
                "receive_decode_time": stamp, "calibration_id": self.calibration["calibration_id"],
            }, [], {"valid": True, "T_map_camera": pose(x), "units": "arbitrary",
                    "map_id": map_id, "map_revision": revision},
            keyframe_id=keyframe_id if exact else None, durable=exact, image_bytes=self.jpeg)
        self.snapshots.append(snapshot)
        self.positions[frame_id] = x
        return snapshot

    def start(self, method="checkerboard", **extra):
        result = self.manager.start_session(self.map_id, self.calibration["calibration_id"], {
            "method": method, "reference": self.reference, **extra})
        return result["session"]["session_id"]

    def detection(self, session, snapshot):
        x = self.positions[snapshot["frame_id"]]
        # The physical board is fixed at z=0. Camera translates in front of it.
        T = pose(x*2, 0, -1)
        points = np.array([[a*0.02,b*0.02,0] for b in range(6) for a in range(9)], float)
        K = np.array([[600,0,640],[0,600,360],[0,0,1]], float)
        image, _ = cv2.projectPoints(points, np.zeros(3), np.array([-x*2,0,1.0]), K, np.zeros(5))
        visibility = {"visible": True, "frame_id": snapshot["frame_id"],
                      "snapshot_id": snapshot["snapshot_id"],
                      "receive_decode_time": snapshot["frame"]["receive_decode_time"],
                      "localization_valid": True, "exact_keyframe": True}
        candidates = [{"T_board_camera": T, "reprojection_rms_px": 0.0,
                       "min_corner_depth_m": 1.0, "corner_order": "detected", "pnp_branch": 0}]
        return visibility, image.reshape(-1,2).tolist(), candidates

    def board_data(self, *, verified=True, auto_apply=False):
        self.reference["size_verified"] = verified
        sid = self.start(auto_apply=auto_apply)
        with patch.object(self.manager, "_detect", side_effect=self.detection):
            for x in [0.0,0.03,0.06,0.09,0.12,0.15]:
                self.manager.offer(self.frame(x))
                self.manager.process_pending()
            while self.manager.process_pending():
                pass
        return sid

    def measured_data(self):
        sid = self.start("measured_distance")
        for label,x in [("start",0.0),("end",1.5),("validation_start",2.0),("validation_end",3.0)]:
            snap = self.frame(x)
            self.manager.capture(sid, snap["snapshot_id"], label=label)
        while self.manager.process_pending():
            pass
        return sid

    def test_automatic_fixed_board_collects_independent_holdout_and_applies(self):
        sid = self.board_data(auto_apply=True)
        status = self.manager.status(sid, include_observations=True)
        self.assertEqual(status["session"]["state"], "applied", status)
        self.assertEqual(status["progress"]["training"], 4)
        self.assertEqual(status["progress"]["validation"], 2)
        self.assertEqual(status["progress"]["accepted"], 6)
        self.assertAlmostEqual(status["metric_scale"]["scale_m_per_map_unit"], 2.0, places=6)
        self.assertFalse(status["motion_commands"])
        self.assertEqual(status["metric_scale"]["floor_alignment"], "unmeasured")
        raw = self.store.read_map(self.map_id)
        self.assertEqual(raw["map"]["metadata"]["units"], "arbitrary")
        metric = self.manager.metric_localization(self.snapshots[-1])
        self.assertTrue(metric["valid"])
        self.assertAlmostEqual(metric["T_map_camera"][0][3], 0.3)
        self.assertIsNone(metric["robot_pose"])
        self.assertIsNone(metric["covariance"])

    def test_nominal_size_can_be_replaced_without_recapturing(self):
        sid = self.board_data(verified=False)
        old = self.manager.status(sid, include_observations=True)
        self.assertEqual(old["progress"]["accepted"], 6)
        self.assertEqual(old["session"]["state"], "provisional", old)
        self.assertEqual(old["session"]["candidate"]["failed_checks"], ["reference_size_unverified"])
        self.assertEqual(self.manager.apply(sid)["availability"], "unavailable")
        updated = self.manager.update_reference(sid, {
            **self.reference, "square_size_m": 0.04, "size_verified": True,
            "measurement_source": "synthetic_fixture_rescaled_physical_reference",
        })
        self.assertEqual(updated["session"]["state"], "ready", updated)
        self.assertEqual(updated["progress"]["accepted"], 6)
        self.assertAlmostEqual(updated["session"]["candidate"]["scale_m_per_map_unit"], 4, places=3)
        result = self.manager.apply(sid)
        self.assertEqual(result["availability"], "available", result)
        with self.assertRaisesRegex(ValueError, "applied"):
            self.manager.update_reference(sid, self.reference)

    def test_measured_three_meter_displacement_needs_independent_validation(self):
        sid = self.measured_data()
        missing = self.manager.fit(sid, {"distance_m": 3.0})
        self.assertFalse(missing["session"]["candidate"]["qualified"])
        self.assertIn("missing_validation_distance_m", missing["guidance"])
        result = self.manager.fit(sid, {"validation_distance_m": 2.0})
        self.assertTrue(result["session"]["candidate"]["qualified"], result)
        scale = self.manager.apply(sid)
        self.assertAlmostEqual(scale["scale_m_per_map_unit"], 2.0)
        self.assertIsNone(scale["candidate"]["R_board_map"])
        self.assertIsNone(scale["candidate"]["t_board_map_m"])

    def test_wrong_independent_distance_blocks_publication(self):
        sid = self.measured_data()
        result = self.manager.fit(sid, {"distance_m": 3.0, "validation_distance_m": 5.0})
        self.assertFalse(result["session"]["candidate"]["qualified"])
        self.assertEqual(self.manager.apply(sid)["availability"], "unavailable")
        self.assertEqual(self.manager.read_metric_map({}, self.map_id)["availability"], "unavailable")

    def test_geometry_correction_refits_reference_and_historical_read_stays_pinned(self):
        sid = self.board_data()
        before = self.manager.apply(sid)
        revision = before["map_revision"]
        raw = self.store.read_map(self.map_id, kinds=["keyframe"])
        changes = []
        for entity in raw["entities"]:
            data = copy.deepcopy(entity["data"])
            data["position"][0] /= 2
            data["T_map_camera"][0][3] /= 2
            changes.append({"op":"upsert", "kind":"keyframe", "entity_id":entity["entity_id"], "data":data})
        self.store.apply_map_update(self.map_id, revision, changes, reason="synthetic_loop_correction")
        after = self.manager.attachment_status(self.map_id)
        self.assertEqual(after["availability"], "available", after)
        self.assertAlmostEqual(after["scale_m_per_map_unit"], 4.0)
        self.assertNotEqual(after["solution_id"], before["solution_id"])
        old = self.manager.attachment_status(self.map_id, revision=revision)
        self.assertEqual(old["solution_id"], before["solution_id"])
        self.assertAlmostEqual(old["scale_m_per_map_unit"], 2.0)
        current = self.manager.read_metric_map({"kinds":["keyframe"]}, self.map_id)
        self.assertAlmostEqual(current["entities"][-1]["data"]["position"][0], 0.3)

    def test_culled_or_mismatched_reference_anchor_withholds_metric_geometry(self):
        sid = self.board_data()
        self.manager.apply(sid)
        revision = self.store.read_map(self.map_id, kinds=[])["map"]["revision"]
        self.store.apply_map_update(self.map_id, revision, [
            {"op":"delete","kind":"keyframe","entity_id":"kf-0"}])
        result = self.manager.read_metric_map({}, self.map_id)
        self.assertEqual(result["availability"], "unavailable")
        self.assertIsNone(result["metric_scale"]["scale_m_per_map_unit"])

    def test_metric_pagination_bounds_and_pixels_preserve_native_data(self):
        sid = self.measured_data()
        self.manager.fit(sid, {"distance_m":3.0,"validation_distance_m":2.0})
        self.manager.apply(sid)
        rev = self.store.read_map(self.map_id,kinds=[])["map"]["revision"]
        self.store.apply_map_update(self.map_id, rev, [
            {"op":"upsert","kind":"sparse_point","entity_id":"p",
             "data":{"xyz":[1.0,2.0,3.0],"rgb":[10,20,30],"units":"arbitrary"}},
            {"op":"upsert","kind":"semantic_observation","entity_id":"s",
             "data":{"detections":{"items":[{"bbox_xyxy_px":[1,2,3,4]}]},
                     "global_positions":None}},
        ])
        page = self.manager.read_metric_map({"limit":1}, self.map_id)
        original_revision = page["map"]["revision"]
        cursor = page["next_cursor"]
        self.assertIsNotNone(cursor)
        current = self.store.read_map(self.map_id,kinds=[])["map"]["revision"]
        self.store.apply_map_update(self.map_id,current,[],metadata={"later":True})
        next_page = self.manager.read_metric_map({"cursor":cursor,"limit":1}, self.map_id)
        self.assertEqual(next_page["map"]["revision"],original_revision)
        with self.assertRaisesRegex(ValueError,"cursor"):
            self.manager.read_metric_map({"cursor":cursor,"bounds":[0,0,10,10]}, self.map_id)
        points = self.manager.read_metric_map(
            {"kinds":["sparse_point"],"bounds":[1.9,3.9,5.9,2.1,4.1,6.1]},self.map_id)
        self.assertEqual(points["entities"][0]["data"]["xyz"],[2.0,4.0,6.0])
        self.assertEqual(points["entities"][0]["data"]["rgb"],[10,20,30])
        semantic = self.manager.read_metric_map({"kinds":["semantic_observation"]},self.map_id)
        self.assertEqual(semantic["entities"][0]["data"]["detections"]["items"][0]["bbox_xyxy_px"],[1,2,3,4])
        native = self.store.read_map(self.map_id,kinds=["sparse_point"])
        self.assertEqual(native["entities"][0]["data"]["xyz"],[1.0,2.0,3.0])
        self.assertEqual(native["map"]["metadata"]["units"],"arbitrary")

    def test_jobs_and_reference_survive_restart_and_checkpoint(self):
        sid = self.start("measured_distance")
        snap = self.frame(0)
        self.manager.capture(sid,snap["snapshot_id"],label="start")
        self.manager.close()
        self.store.close()
        self.store = VisionStore(self.root,max_observations=2)
        self.manager = MetricCalibration(self.store)
        self.assertEqual(self.manager.status(sid)["progress"]["queued"],1)
        self.manager.process_pending()
        self.assertEqual(self.manager.status(sid)["progress"]["accepted"],1)
        self.assertEqual(self.store.get_frame(snap["frame_id"],include_data=True)["availability"],"available")
        saved = self.store.checkpoint(self.map_id)
        db = sqlite3.connect(saved["database_path"])
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM metric_sessions").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM metric_samples").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM metric_jobs WHERE state='accepted'").fetchone()[0],1)
        finally:
            db.close()

    def test_applied_reference_survives_restart_without_inventing_current_pose(self):
        sid = self.measured_data()
        self.manager.fit(sid,{"distance_m":3.0,"validation_distance_m":2.0})
        attachment = self.manager.apply(sid)
        self.manager.close()
        self.store.close()
        self.store = VisionStore(self.root,max_observations=2)
        self.manager = MetricCalibration(self.store)
        restored = self.manager.attachment_status(self.map_id)
        self.assertEqual(restored["attachment_id"],attachment["attachment_id"])
        self.assertAlmostEqual(restored["scale_m_per_map_unit"],2.0)
        unknown = {**self.snapshots[-1],"localization":{"valid":False,"T_map_camera":None}}
        result = self.manager.metric_localization(unknown)
        self.assertFalse(result["valid"])
        self.assertIsNone(result["T_map_camera"])

    def test_non_keyframe_and_source_frame_mismatch_cannot_be_captured(self):
        sid = self.start("measured_distance")
        nonkey = self.frame(0,exact=False)
        with self.assertRaisesRegex(ValueError,"exact_localized_keyframe"):
            self.manager.capture(sid,nonkey["snapshot_id"],label="start")
        snap = self.frame(1)
        # An entity claiming another image is not an exact source anchor.
        raw = self.store.read_map(self.map_id,kinds=["keyframe"])
        data = copy.deepcopy(raw["entities"][0]["data"])
        data["source_frame_id"] = "unrelated"
        self.store.apply_map_update(self.map_id,raw["map"]["revision"],[
            {"op":"upsert","kind":"keyframe","entity_id":snap["keyframe_id"],"data":data}])
        self.manager.capture(sid,snap["snapshot_id"],label="start")
        self.manager.process_pending()
        result = self.manager.fit(sid,{"distance_m":3.0,"validation_distance_m":3.0})
        self.assertFalse(result["session"]["candidate"]["qualified"])
        self.assertEqual(result["session"]["candidate"]["status"],"stale")

    def test_cancel_prevents_queued_capture_and_new_map_cannot_inherit_scale(self):
        sid = self.start("measured_distance")
        snap = self.frame(0)
        self.manager.capture(sid,snap["snapshot_id"],label="start")
        self.manager.cancel(sid)
        self.manager.process_pending()
        self.assertEqual(self.manager.status(sid)["progress"]["accepted"],0)
        with self.assertRaisesRegex(ValueError,"canceled"):
            self.manager.capture(sid,snap["snapshot_id"],label="start")
        other = self.store.create_map({"calibration_id":self.calibration["calibration_id"]})["map_id"]
        self.assertEqual(self.manager.attachment_status(other)["availability"],"unavailable")

    def test_invalid_policy_reference_and_distance_are_rejected_before_application(self):
        for value in [False,0,-1,float("nan"),float("inf")]:
            with self.assertRaises(ValueError):
                self.start(reference={**self.reference,"square_size_m":value})
        for pattern in ([2, 6], [6, 6]):
            with self.assertRaises(ValueError):
                self.start(reference={**self.reference,"inner_corners":pattern})
        with self.assertRaises(ValueError):
            self.start(policy={"invented_gate":1})
        with self.assertRaisesRegex(ValueError,"stationary"):
            self.start(reference={**self.reference,"stationary":False})
        sid = self.start("measured_distance")
        for value in [False,0,-1,float("nan"),float("inf")]:
            with self.assertRaises(ValueError):
                self.manager.fit(sid,{"distance_m":value})

    def test_target_visibility_does_not_require_localized_keyframe(self):
        sid = self.start()
        snap = self.frame(0,exact=False)
        self.manager.offer(snap)
        with patch.object(self.manager,"_detect",side_effect=self.detection):
            self.manager.process_pending()
        status = self.manager.status(sid)
        self.assertTrue(status["target"]["visible"])
        self.assertEqual(status["progress"]["accepted"],0)
        self.assertIsNone(status["latest_keyframe"])
        self.assertTrue(status["target"]["matches_latest_frame"])

    def test_real_checkerboard_detector_preserves_calibrated_image_coordinates(self):
        image = np.full((720,1280,3), 255, np.uint8)
        for row in range(7):
            for col in range(10):
                if (row+col)%2 == 0:
                    image[200+row*30:200+(row+1)*30, 400+col*30:400+(col+1)*30] = 0
        ok, encoded = cv2.imencode(".jpg", image)
        self.assertTrue(ok)
        self.jpeg = encoded.tobytes()
        sid = self.start()
        snapshot = self.frame(0)
        visibility, corners, candidates = self.manager._detect(self.manager._session(sid), snapshot)
        self.assertTrue(visibility["visible"])
        self.assertEqual(len(corners), 54)
        self.assertGreater(len(candidates), 0)
        self.assertEqual(visibility["image_size_px"], [1280,720])
        self.assertTrue(visibility["board_center_range"]["estimate"] > 0)

    def test_interrupted_automatic_fit_or_application_recovers_from_accepted_evidence(self):
        for boundary in ("fit", "application"):
            with self.subTest(boundary=boundary):
                sid = self.start(auto_apply=True)
                with patch.object(self.manager, "_detect", side_effect=self.detection):
                    for x in [0.0,0.03,0.06,0.09,0.12]:
                        self.manager.offer(self.frame(x))
                        self.manager.process_pending()
                    self.manager.offer(self.frame(0.15))
                    target = self.manager if boundary == "fit" else self.manager.repo
                    method = "_fit_locked" if boundary == "fit" else "save_attachment"
                    with patch.object(target, method, side_effect=RuntimeError("synthetic_interrupt")):
                        with self.assertRaisesRegex(RuntimeError, "synthetic_interrupt"):
                            self.manager.process_pending()
                persisted = self.manager.repo.get_session(sid)
                self.assertEqual(persisted["state"], "collecting")
                self.assertEqual(len(self.manager.repo.samples(sid)), 6)
                self.manager.close()
                self.store.close()
                self.store = VisionStore(self.root, max_observations=2)
                self.manager = MetricCalibration(self.store)
                recovered = self.manager.status(sid)
                self.assertEqual(recovered["session"]["state"], "applied", recovered)
                self.assertAlmostEqual(recovered["metric_scale"]["scale_m_per_map_unit"], 2.0)

    def test_rejected_and_canceled_jobs_release_only_their_own_pins(self):
        sid = self.start()
        snap = self.frame(0)
        self.store.pin_frame(snap["frame_id"], "independent-keyframe-owner")
        self.manager.offer(snap)
        with patch.object(self.manager, "_detect", return_value=(
                {"visible":False,"frame_id":snap["frame_id"]}, None, [])):
            self.manager.process_pending()
        with self.store._lock:
            pins = list(self.store._db.execute("SELECT reason FROM frame_pins WHERE frame_id=?",
                                              (snap["frame_id"],)))
        self.assertEqual([row[0] for row in pins], ["independent-keyframe-owner"])
        self.assertEqual(self.manager.status(sid)["progress"]["rejected"],1)
        other = self.frame(0.1)
        self.manager.offer(other)
        self.manager.cancel(sid)
        while self.manager.process_pending():
            pass
        with self.store._lock:
            count = self.store._db.execute("SELECT COUNT(*) FROM frame_pins WHERE reason=?",
                                           ("metric:"+sid,)).fetchone()[0]
        self.assertEqual(count,0)



    def _surplus_board_data(self, verified=True):
        sid = self.board_data(verified=verified)
        with patch.object(self.manager, "_detect", side_effect=self.detection):
            extra = self.frame(0.18)
            self.manager.capture(sid, extra["snapshot_id"], role="training")
            while self.manager.process_pending():
                pass
        return sid

    def _delete_anchor(self, identifier):
        revision = self.store.read_map(self.map_id, kinds=[])["map"]["revision"]
        return self.store.apply_map_update(self.map_id, revision, [
            {"op":"delete", "kind":"keyframe", "entity_id":identifier}])["revision"]

    def test_deleted_checkerboard_anchor_refits_survivors_and_preserves_history(self):
        sid = self._surplus_board_data()
        before = self.manager.apply(sid)
        self.assertEqual(before["availability"], "available", before)
        deletion = self._delete_anchor("kf-0")
        after = self.manager.attachment_status(self.map_id)
        self.assertEqual(after["availability"], "available", after)
        self.assertAlmostEqual(after["scale_m_per_map_unit"], 2.0)
        resolution = after["candidate"]["anchor_resolution"]
        self.assertEqual((resolution["usable_training"],resolution["usable_validation"]),(4,2))
        self.assertEqual(len(resolution["excluded_observations"]),1)
        self.assertEqual(resolution["excluded_observations"][0]["deletion_revision"],deletion)
        self.assertEqual(resolution["excluded_observations"][0]["reason"],"anchor_deleted")
        self.assertEqual(len(self.manager.repo.samples(sid)),7)
        old = self.manager.attachment_status(self.map_id,revision=before["map_revision"])
        self.assertEqual(old["solution_id"],before["solution_id"])
        self.assertEqual(old["candidate"]["anchor_resolution"]["usable_observations"],7)

    def test_deleted_anchor_does_not_verify_nominal_print_size(self):
        sid = self._surplus_board_data(verified=False)
        self._delete_anchor("kf-0")
        result = self.manager.fit(sid,{})
        self.assertFalse(result["session"]["candidate"]["qualified"])
        self.assertIn("reference_size_unverified",result["session"]["candidate"]["failed_checks"])
        self.assertEqual(self.manager.apply(sid)["availability"],"unavailable")

    def test_tombstone_for_unknown_or_conflicting_history_stays_unresolved(self):
        sid = self._surplus_board_data()
        sample = copy.deepcopy(self.manager.repo.samples(sid)[0])
        original = copy.deepcopy(sample)
        self._delete_anchor(original["keyframe_id"])
        self._delete_anchor("never-existed")
        for change in [
            {"keyframe_id":"never-existed"},
            {"frame_id":"wrong-image"},
            {"source_map_revision":999999},
            {"map_id":"foreign-map"},
            {"calibration_id":"foreign-calibration"},
        ]:
            with self.subTest(change=change):
                altered = {**original, **change}
                _, _, missing, excluded = self.manager.repo.resolve_keyframes_for_fit(
                    self.map_id,[altered],exclude_deleted=True)
                self.assertEqual(missing,[original["observation_id"]])
                self.assertEqual(excluded,[])

    def test_reused_keyframe_identity_before_deletion_is_not_excludable(self):
        sid = self._surplus_board_data()
        sample = self.manager.repo.samples(sid)[0]
        revision = self.store.read_map(self.map_id,kinds=[])["map"]["revision"]
        self.store.apply_map_update(self.map_id,revision,[{
            "op":"upsert","kind":"keyframe","entity_id":sample["keyframe_id"],
            "data":{"source_frame_id":"replacement-image","T_map_camera":pose(),
                    "position":[0,0,0],"units":"arbitrary"},
        }])
        self._delete_anchor(sample["keyframe_id"])
        _, _, missing, excluded = self.manager.repo.resolve_keyframes_for_fit(
            self.map_id,[sample],exclude_deleted=True)
        self.assertEqual(missing,[sample["observation_id"]])
        self.assertEqual(excluded,[])

    def test_delete_reinsert_delete_does_not_hide_incarnation_ambiguity(self):
        sid = self._surplus_board_data()
        sample = self.manager.repo.samples(sid)[0]
        self._delete_anchor(sample["keyframe_id"])
        revision = self.store.read_map(self.map_id,kinds=[])["map"]["revision"]
        self.store.apply_map_update(self.map_id,revision,[{
            "op":"upsert","kind":"keyframe","entity_id":sample["keyframe_id"],
            "data":{"source_frame_id":sample["frame_id"],"T_map_camera":pose(),
                    "position":[0,0,0],"units":"arbitrary"},
        }])
        self._delete_anchor(sample["keyframe_id"])
        _, _, missing, excluded = self.manager.repo.resolve_keyframes_for_fit(
            self.map_id,[sample],exclude_deleted=True)
        self.assertEqual(missing,[sample["observation_id"]])
        self.assertEqual(excluded,[])

    def test_measured_endpoint_deletion_stays_strict(self):
        sid = self.measured_data()
        self.manager.fit(sid,{"distance_m":3.0,"validation_distance_m":2.0})
        self.assertEqual(self.manager.apply(sid)["availability"],"available")
        self._delete_anchor("kf-0")
        result = self.manager.attachment_status(self.map_id)
        self.assertEqual(result["availability"],"unavailable")
        resolution = result["candidate"]["anchor_resolution"]
        self.assertEqual(resolution["excluded_observations"],[])
        self.assertEqual(len(resolution["unresolved_observation_ids"]),1)


if __name__ == "__main__":
    unittest.main()
