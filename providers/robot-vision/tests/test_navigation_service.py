"""Exercise frame-attachment consumption through the real vision service/store."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_vision_service as fixtures
from navigation_frames import BINDING, read_robot_pose
from vision_config import atomic_json


class NavigationServiceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.VisionServiceTests("runTest")
        self.fixture.setUp()
        self.service = self.fixture.make_service()
        self.snapshot = self.fixture.process(
            self.service, self.fixture.frame(self.service, "robot-pose"))
        self.path = self.service.state_dir / "navigation-frames.json"
        self.floor = {
            "schema_version": 1, "kind": "map_floor_alignment",
            "attachment_id": "fixture-floor", "revision": 1, "units": "m",
            "from_frame": "map_metric", "to_frame": "floor",
            "map_id": self.snapshot["map_id"], "map_revision": self.snapshot["map_revision"],
            "calibration_id": self.snapshot["calibration_id"],
            "metric_attachment_id": "fixture-scale", "metric_solution_id": "fixture-solution",
            "transform": fixtures.identity_pose(),
            "qualification": {"status": "qualified", "method": "synthetic_fixture",
                              "evidence": ["fixture-floor-measurement"]}}
        self.mount = {
            "schema_version": 1, "kind": "camera_robot_extrinsics",
            "attachment_id": "fixture-mount", "revision": 1, "units": "m",
            "from_frame": "robot", "to_frame": "camera_optical",
            "calibration_id": self.snapshot["calibration_id"],
            "physical_mount_id": "fixture-mounting", "robot_model_id": "fixture-robot",
            "transform": fixtures.identity_pose(),
            "qualification": {"status": "qualified", "method": "synthetic_fixture",
                              "evidence": ["fixture-rig-measurement"]}}
        self.mount["transform"][0][3] = 0.25

    def tearDown(self):
        self.fixture.tearDown()

    def write(self):
        atomic_json(self.path, {"schema_version": 1, "floor_alignment": self.floor,
                               "camera_robot_extrinsics": self.mount})

    def metric(self, snapshot):
        pose = fixtures.identity_pose()
        pose[0][3] = 2.0
        return {**{key: snapshot[key] for key in BINDING},
                "availability": "available", "valid": True, "units": "m",
                "coordinate_frame": "map_metric", "pose_origin": "camera_optical",
                "attachment_id": "fixture-scale", "solution_id": "fixture-solution",
                "T_map_camera": pose}

    def test_missing_configuration_preserves_original_observation_and_metric_status(self):
        response = self.service.query("robot.vision.snapshot")["snapshot"]
        self.assertEqual(response["frame_id"], self.snapshot["frame_id"])
        self.assertTrue(response["localization"]["valid"])
        self.assertFalse(response["metric_localization"]["valid"])
        self.assertEqual(response["robot_localization"]["reason"], "navigation_frames_not_configured")
        self.assertEqual(self.service.store.get_snapshot(self.snapshot["snapshot_id"]), self.snapshot)

    def test_qualified_pose_reaches_snapshot_frame_and_geometry_with_exact_binding(self):
        self.write()
        with patch.object(self.service.metric, "metric_localization", side_effect=self.metric):
            response = self.service.query("robot.vision.snapshot", {"include_frame": True})
            geometry = self.service.query("robot.vision.geometry")
        robot = response["snapshot"]["robot_localization"]
        self.assertTrue(robot["valid"], robot)
        self.assertEqual(robot["translation_m"], [2.25, 0, 0])
        self.assertEqual(robot["frame_id"], response["frame"]["frame_id"])
        for key in (*BINDING, "T_floor_robot", "floor_alignment", "camera_robot_extrinsics"):
            self.assertEqual(geometry["robot_localization"][key], robot[key])
        self.assertEqual(response["snapshot"]["localization"]["units"], "arbitrary")
        self.assertEqual(response["snapshot"]["metric_localization"]["pose_origin"], "camera_optical")
        self.assertFalse(robot["navigation_available"])
        self.assertEqual(self.service.store.get_snapshot(self.snapshot["snapshot_id"]), self.snapshot)

    def test_attachment_revision_can_update_without_map_or_session_restart(self):
        self.write()
        identity = (self.service.map_id, self.service.session_id, self.service.calibration["calibration_id"])
        with patch.object(self.service.metric, "metric_localization", side_effect=self.metric):
            first = self.service.query("robot.vision.snapshot")["snapshot"]["robot_localization"]
            self.mount["revision"] = 2
            self.mount["transform"][0][3] = 0.5
            self.write()
            second = self.service.query("robot.vision.snapshot")["snapshot"]["robot_localization"]
        self.assertEqual(first["camera_robot_extrinsics"]["revision"], 1)
        self.assertEqual(second["camera_robot_extrinsics"]["revision"], 2)
        self.assertEqual(second["translation_m"], [2.5, 0, 0])
        self.assertEqual(identity, (self.service.map_id, self.service.session_id,
                                    self.service.calibration["calibration_id"]))

    def test_map_revision_change_withholds_robot_pose_but_preserves_camera_pose(self):
        self.floor["map_revision"] += 1
        self.write()
        with patch.object(self.service.metric, "metric_localization", side_effect=self.metric):
            response = self.service.query("robot.vision.geometry")
        self.assertEqual(response["robot_localization"]["reason"], "floor_alignment_anchor_resolution_failed")
        self.assertTrue(response["localization"]["valid"])
        self.assertTrue(response["metric_localization"]["valid"])

    def test_no_metric_scale_cannot_be_replaced_by_qualified_mounting(self):
        self.write()
        response = self.service.query("robot.vision.snapshot")["snapshot"]
        self.assertEqual(response["robot_localization"]["reason"], "metric_camera_pose_unavailable")
        self.assertFalse(response["robot_localization"]["valid"])

    def test_stale_snapshot_never_becomes_a_current_robot_pose(self):
        self.write()
        old = self.fixture.process(
            self.service, self.fixture.frame(self.service, "stale-pose", stale=True))
        self.floor["map_revision"] = old["map_revision"]
        self.write()
        with patch.object(self.service.metric, "metric_localization", side_effect=self.metric):
            robot = self.service.query("robot.vision.snapshot")["snapshot"]["robot_localization"]
        self.assertEqual(robot["reason"], "snapshot_stale_or_unqualified")

    def test_previous_session_snapshot_is_not_current_after_restart(self):
        self.write()
        self.service.close()
        restored = self.fixture.make_service()
        with patch.object(restored.metric, "metric_localization", side_effect=self.metric):
            response = restored.query("robot.vision.snapshot",
                                      {"snapshot_id": self.snapshot["snapshot_id"]})["snapshot"]
        self.assertFalse(response["freshness"]["is_current_session"])
        self.assertEqual(response["robot_localization"]["reason"], "snapshot_stale_or_unqualified")

    def test_invalid_optional_configuration_cannot_break_snapshot_queries(self):
        invalid = ("{", "[]", '{"schema_version":true}',
                   '{"schema_version":1,"schema_version":2}',
                   '{"schema_version":1,"bad":NaN}', " " * 65537)
        for content in invalid:
            with self.subTest(content=content[:40]):
                self.path.write_text(content)
                response = self.service.query("robot.vision.snapshot")["snapshot"]
                self.assertEqual(response["robot_localization"]["reason"],
                                 "navigation_frames_configuration_invalid")
                self.assertTrue(response["localization"]["valid"])
                self.assertEqual(response["frame_id"], self.snapshot["frame_id"])

    def test_unreadable_optional_attachment_is_unavailable_without_restart(self):
        self.path.mkdir()
        response = self.service.query("robot.vision.geometry")
        self.assertEqual(response["robot_localization"]["reason"],
                         "navigation_frames_configuration_invalid")
        self.assertTrue(response["localization"]["valid"])

    def test_persistence_contains_no_query_derived_robot_pose(self):
        self.write()
        with patch.object(self.service.metric, "metric_localization", side_effect=self.metric):
            self.assertTrue(self.service.query("robot.vision.snapshot")["snapshot"]["robot_localization"]["valid"])
        self.assertNotIn("robot_localization", self.service.store.get_snapshot(self.snapshot["snapshot_id"]))

    def test_loader_does_not_echo_configuration_contents_on_error(self):
        self.path.write_text('{"schema_version":1,"private":"DO_NOT_ECHO"')
        result = read_robot_pose(self.snapshot, self.path, delivery_elapsed_s=0)
        self.assertNotIn("DO_NOT_ECHO", json.dumps(result))
        self.assertFalse(result["valid"])

    def anchored_floor(self):
        captured = self.fixture.frame(self.service, "qualified-floor-anchor")
        frame_id = captured.metadata["frame_id"]
        transform = fixtures.identity_pose()
        self.service.backend.scripted.append({
            "localization": {"valid": True, "status": "tracking",
                             "T_map_camera": transform, "units": "arbitrary"},
            "geometry": {"available": True, "complete_snapshot": False},
            "map_changes": [{"op": "upsert", "kind": "keyframe", "entity_id": "floor-anchor",
                             "data": {"T_map_camera": transform, "position": [0,0,0],
                                      "source_frame_id": frame_id, "units": "arbitrary"}}]})
        reference = self.fixture.process(self.service, captured)
        self.floor["map_revision"] = reference["map_revision"]
        self.floor["supporting_keyframes"] = [{
            "keyframe_id": "floor-anchor", "source_frame_id": frame_id,
            "T_map_camera": transform}]
        self.write()
        return reference

    def advance_map(self, changes):
        update = self.service.store.apply_map_update(
            self.service.map_id, self.service.revision, changes)
        self.service.revision = update["revision"]
        return self.fixture.process(
            self.service, self.fixture.frame(self.service, "after-map-update"))

    def test_unrelated_semantic_revision_preserves_alignment_with_native_anchor_proof(self):
        reference = self.anchored_floor()
        latest = self.advance_map([{
            "op": "upsert", "kind": "semantic_observation", "entity_id": "independent-label",
            "data": {"frame_id": "independent-label", "detections": []}}])
        with patch.object(self.service.metric, "metric_localization", side_effect=self.metric):
            robot = self.service.query("robot.vision.snapshot")["snapshot"]["robot_localization"]
        self.assertTrue(robot["valid"], robot)
        proof = robot["floor_alignment"]["resolution"]
        self.assertEqual(proof["qualified_map_revision"], reference["map_revision"])
        self.assertEqual(proof["resolved_map_revision"], latest["map_revision"])
        self.assertEqual(proof["rule"], "identical_native_keyframe_versions")
        self.assertEqual(proof["anchors"][0]["keyframe_id"], "floor-anchor")
        self.assertEqual(json.loads(self.path.read_text())["floor_alignment"]["map_revision"],
                         reference["map_revision"])

    def test_unrelated_sparse_landmark_update_preserves_alignment(self):
        self.anchored_floor()
        self.advance_map([{"op": "upsert", "kind": "sparse_point", "entity_id": "new-point",
                           "data": {"xyz": [1,2,3], "units": "arbitrary"}}])
        with patch.object(self.service.metric, "metric_localization", side_effect=self.metric):
            robot = self.service.query("robot.vision.geometry")["robot_localization"]
        self.assertTrue(robot["valid"], robot)

    def test_floor_anchor_correction_withholds_robot_pose(self):
        self.anchored_floor()
        anchor = self.floor["supporting_keyframes"][0]
        corrected = copy.deepcopy(anchor["T_map_camera"])
        corrected[0][3] = 0.2
        self.advance_map([{"op": "upsert", "kind": "keyframe", "entity_id": "floor-anchor",
                           "data": {"T_map_camera": corrected,
                                    "source_frame_id": anchor["source_frame_id"]}}])
        with patch.object(self.service.metric, "metric_localization", side_effect=self.metric):
            response = self.service.query("robot.vision.geometry")
        self.assertFalse(response["robot_localization"]["valid"])
        self.assertEqual(response["robot_localization"]["reason"], "floor_alignment_anchor_resolution_failed")
        self.assertTrue(response["metric_localization"]["valid"])

    def test_deleted_then_recreated_anchor_cannot_reuse_original_floor_qualification(self):
        self.anchored_floor()
        anchor = self.floor["supporting_keyframes"][0]
        self.advance_map([{"op": "delete", "kind": "keyframe", "entity_id": "floor-anchor",
                           "data": {}}])
        self.advance_map([{"op": "upsert", "kind": "keyframe", "entity_id": "floor-anchor",
                           "data": {"T_map_camera": anchor["T_map_camera"],
                                    "source_frame_id": anchor["source_frame_id"]}}])
        with patch.object(self.service.metric, "metric_localization", side_effect=self.metric):
            robot = self.service.query("robot.vision.geometry")["robot_localization"]
        self.assertFalse(robot["valid"])
        self.assertEqual(robot["reason"], "floor_alignment_anchor_resolution_failed")

    def test_anchor_record_must_match_the_original_native_source_pose(self):
        self.anchored_floor()
        self.floor["supporting_keyframes"][0]["T_map_camera"][0][3] = 9
        self.write()
        self.advance_map([{"op": "upsert", "kind": "sparse_point", "entity_id": "later-point",
                           "data": {"xyz": [1,0,0], "units": "arbitrary"}}])
        with patch.object(self.service.metric, "metric_localization", side_effect=self.metric):
            robot = self.service.query("robot.vision.geometry")["robot_localization"]
        self.assertFalse(robot["valid"])

    def test_unchanged_anchor_cannot_cross_a_scale_solution_change(self):
        self.anchored_floor()
        self.advance_map([{"op": "upsert", "kind": "sparse_point", "entity_id": "later-point",
                           "data": {"xyz": [1,0,0], "units": "arbitrary"}}])
        original_metric = self.metric
        def refitted(snapshot):
            result = original_metric(snapshot)
            result["solution_id"] = "new-scale-solution"
            return result
        with patch.object(self.service.metric, "metric_localization", side_effect=refitted):
            robot = self.service.query("robot.vision.geometry")["robot_localization"]
        self.assertFalse(robot["valid"])
        self.assertEqual(robot["reason"], "floor_alignment_binding_mismatch")



if __name__ == "__main__":
    unittest.main()
