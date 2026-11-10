"""Geometry regressions with known noncommuting transforms and stale bindings."""
import copy
import importlib.util
import math
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location(
    "navigation_frames", Path(__file__).resolve().parents[1] / "navigation_frames.py")
frames = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(frames)
I = [[1,0,0,0], [0,1,0,0], [0,0,1,0], [0,0,0,1]]
RZ90 = [[0,-1,0,0], [1,0,0,0], [0,0,1,0], [0,0,0,1]]


class NavigationFramesTests(unittest.TestCase):
    def setUp(self):
        binding = {"snapshot_id": "snap", "frame_id": "frame", "map_id": "map",
                   "map_revision": 7, "calibration_id": "lens"}
        self.snapshot = {**binding, "session_id": "live-session",
                         "freshness": {"is_fresh": True, "is_current_session": True,
                                       "receive_decode_age_s": 0.1, "freshness_threshold_s": 2},
                         "metric_localization": {
                             **binding, "availability": "available", "valid": True,
                             "units": "m", "coordinate_frame": "map_metric",
                             "pose_origin": "camera_optical", "attachment_id": "scale",
                             "solution_id": "scale-solution", "T_map_camera": copy.deepcopy(I)}}
        self.floor = {"schema_version": 1, "kind": "map_floor_alignment",
                      "attachment_id": "floor", "revision": 1, "units": "m",
                      "from_frame": "map_metric", "to_frame": "floor",
                      "map_id": "map", "map_revision": 7, "calibration_id": "lens",
                      "metric_attachment_id": "scale", "metric_solution_id": "scale-solution",
                      "transform": copy.deepcopy(I),
                      "qualification": {"status": "qualified", "method": "fixture_plane",
                                        "evidence": ["fixture-plane-1"]}}
        self.mount = {"schema_version": 1, "kind": "camera_robot_extrinsics",
                      "attachment_id": "mount", "revision": 3, "units": "m",
                      "from_frame": "robot", "to_frame": "camera_optical",
                      "calibration_id": "lens", "physical_mount_id": "mounting-1",
                      "robot_model_id": "rear-steering-robot", "transform": copy.deepcopy(I),
                      "qualification": {"status": "qualified", "method": "fixture_rig",
                                        "evidence": ["fixture-rig-1"]}}

    def pose(self, **kwargs):
        return frames.derive_robot_pose(self.snapshot, self.floor, self.mount,
                                        delivery_elapsed_s=kwargs.get("delivery_elapsed_s", 0.02))

    def test_identity_is_pose_but_not_navigation(self):
        result = self.pose()
        self.assertTrue(result["valid"])
        self.assertEqual(result["T_floor_robot"], I)
        self.assertFalse(result["navigation_available"])
        self.assertIsNone(result["covariance"])
        self.assertIsNone(result["velocity"])
        self.assertIsNone(result["occupancy"])
        self.assertFalse(result["source_exposure_time_qualified"])

    def test_noncommuting_rotations_and_mount_lever_arm(self):
        self.floor["transform"] = copy.deepcopy(RZ90)
        self.floor["transform"][0][3] = 10
        camera = copy.deepcopy(RZ90)
        camera[0][3] = 2
        self.snapshot["metric_localization"]["T_map_camera"] = camera
        self.mount["transform"][0][3] = 1
        result = self.pose()
        self.assertTrue(result["valid"])
        self.assertEqual(result["translation_m"], [9,2,0])
        self.assertAlmostEqual(abs(result["heading_rad"]), math.pi)

    def test_camera_optical_axes_convert_to_robot_forward_left_up(self):
        self.snapshot["metric_localization"]["T_map_camera"] = [
            [0,0,1,1], [-1,0,0,2], [0,-1,0,0.1], [0,0,0,1]]
        self.mount["transform"] = [
            [0,-1,0,0], [0,0,-1,0], [1,0,0,0], [0,0,0,1]]
        result = self.pose()
        self.assertTrue(result["valid"])
        self.assertEqual(result["translation_m"], [1,2,0.1])
        self.assertEqual([row[:3] for row in result["T_floor_robot"][:3]],
                         [[1,0,0], [0,1,0], [0,0,1]])
        self.assertEqual(result["heading_rad"], 0)

    def test_scale_is_not_applied_again(self):
        self.snapshot["metric_localization"]["T_map_camera"][0][3] = 3.25
        self.assertEqual(self.pose()["translation_m"], [3.25,0,0])

    def test_map_revision_correction_invalidates_floor_attachment(self):
        self.snapshot["map_revision"] = 8
        self.snapshot["metric_localization"]["map_revision"] = 8
        self.assertEqual(self.pose()["reason"], "floor_alignment_binding_mismatch")

    def test_scale_refit_invalidates_previous_floor_solution(self):
        self.snapshot["metric_localization"]["solution_id"] = "corrected-scale"
        self.assertEqual(self.pose()["reason"], "floor_alignment_binding_mismatch")

    def test_metric_snapshot_binding_must_match_exactly(self):
        for key in frames.BINDING:
            with self.subTest(key=key):
                old = self.snapshot["metric_localization"][key]
                self.snapshot["metric_localization"][key] = 0 if key == "map_revision" else "old"
                self.assertEqual(self.pose()["reason"], "metric_camera_pose_binding_mismatch")
                self.snapshot["metric_localization"][key] = old

    def test_metric_pose_must_be_camera_optical_in_metres(self):
        metric = self.snapshot["metric_localization"]
        for key, value in (("units", "arbitrary"), ("valid", 1),
                           ("coordinate_frame", "floor"), ("pose_origin", "robot")):
            with self.subTest(key=key):
                old = metric[key]
                metric[key] = value
                self.assertFalse(self.pose()["valid"])
                metric[key] = old

    def test_expired_transport_age_and_invalid_clocks_are_rejected(self):
        for elapsed in (2, -1, True, float("nan"), float("inf")):
            with self.subTest(elapsed=elapsed):
                self.assertFalse(self.pose(delivery_elapsed_s=elapsed)["valid"])
        self.snapshot["freshness"]["is_current_session"] = False
        self.assertFalse(self.pose()["valid"])

    def test_unmeasured_mount_and_floor_are_not_inferred(self):
        for record in (self.mount, self.floor):
            with self.subTest(kind=record["kind"]):
                record["qualification"]["status"] = "provisional"
                self.assertFalse(self.pose()["valid"])
                record["qualification"]["status"] = "qualified"

    def test_qualification_needs_a_method_and_evidence(self):
        for qualification in ({}, {"status": "qualified"}, {
                "status": "qualified", "method": "measured", "evidence": [""]}):
            self.mount["qualification"] = qualification
            self.assertFalse(self.pose()["valid"])

    def test_mount_is_bound_to_calibration_and_physical_identity(self):
        for key in ("calibration_id", "physical_mount_id", "robot_model_id"):
            with self.subTest(key=key):
                old = self.mount[key]
                self.mount[key] = ""
                self.assertFalse(self.pose()["valid"])
                self.mount[key] = old

    def test_matrix_rejects_scale_reflection_nonfinite_and_bad_last_row(self):
        matrices = []
        for row, column, value in ((0,0,2), (0,0,-1), (1,3,float("nan")), (3,3,0)):
            matrix = copy.deepcopy(I)
            matrix[row][column] = value
            matrices.append(matrix)
        matrices += [[], None, [[1]*4]*4]
        for matrix in matrices:
            with self.subTest(matrix=matrix):
                self.floor["transform"] = matrix
                result = self.pose()
                self.assertFalse(result["valid"])
                self.assertIsNone(result["T_floor_robot"])

    def test_vertical_forward_axis_has_no_floor_heading(self):
        self.mount["transform"] = [[0,0,-1,0], [0,1,0,0], [1,0,0,0], [0,0,0,1]]
        result = self.pose()
        self.assertTrue(result["valid"])
        self.assertIsNone(result["heading_rad"])
        self.assertEqual(result["heading_status"], "robot_forward_axis_vertical")

    def test_records_and_pose_inputs_remain_unchanged(self):
        before = copy.deepcopy((self.snapshot, self.floor, self.mount))
        result = self.pose()
        self.assertEqual(before, (self.snapshot, self.floor, self.mount))
        result["T_floor_robot"][0][3] = 99
        self.assertEqual(before, (self.snapshot, self.floor, self.mount))

    def test_missing_geometry_does_not_substitute_sensor_evidence(self):
        self.snapshot["spatial_evidence"] = {"ahrs": {"valid": True}, "radio": {"valid": True}}
        self.snapshot["metric_localization"] = None
        self.assertFalse(self.pose()["valid"])
        self.assertFalse(frames.derive_robot_pose(
            self.snapshot, None, None, delivery_elapsed_s=0)["valid"])


if __name__ == "__main__":
    unittest.main()
