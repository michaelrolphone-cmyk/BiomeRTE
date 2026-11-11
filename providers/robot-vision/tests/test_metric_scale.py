import copy
import json
import unittest
import cv2 as cv
import numpy as np

from metric_scale import (
    DEFAULT_POLICY, board_pose_candidates, estimate_displacement_scale,
    estimate_metric_attachment, validate_policy,
)

REFERENCE = {"inner_corners": [9, 6], "square_size_m": .02, "size_verified": True,
             "measurement_source": "synthetic exact board geometry", "stationary": True}

def rotation(vector):
    return cv.Rodrigues(np.asarray(vector, dtype=float))[0]

def transform(R, position):
    T = np.eye(4)
    T[:3, :3], T[:3, 3] = R, position
    return T

def observations(count=10, scale=2.4, *, collinear=False):
    R, t = rotation([.2, -.1, .3]), np.array([.25, -.1, .4])
    Rbc = rotation([.04, .08, -.02])
    result = []
    for i in range(count):
        Cb = np.array([.055*i, .03 if collinear else .025*np.sin(.4*i), -1.0-.011*i])
        Cm = R.T @ (Cb-t) / scale
        Tbc = transform(Rbc, Cb)
        result.append({
            "observation_id": f"o{i}", "frame_id": f"f{i}", "snapshot_id": f"o{i}",
            "map_id": "map-a", "calibration_id": "cal-a", "reference_board_id": "board-a",
            "keyframe_id": f"k{i}", "source_map_revision": i, "resolved_map_revision": 20,
            "T_map_camera": transform(R.T @ Rbc, Cm).tolist(),
            "reference": copy.deepcopy(REFERENCE),
            "board_pose_candidates": [{
                "T_board_camera": Tbc.tolist(), "reprojection_rms_px": .2,
                "min_corner_depth_m": .8, "corner_order": "detected", "pnp_branch": 0,
            }],
        })
    return result, scale, R, t

def fit(values, validation=("o7", "o9"), **kwargs):
    return estimate_metric_attachment(values, validation_sample_ids=list(validation), **kwargs)

class BoardPoseTests(unittest.TestCase):
    def test_real_opencv_projected_corners_recover_camera_center_and_both_orders(self):
        calibration = {
            "validated": True, "lens_model": "opencv_pinhole",
            "fx": 950., "fy": 955., "cx": 640., "cy": 360.,
            "width": 1280, "height": 720, "distortion": [.01, -.01, .001, -.002, .005],
        }
        obj = np.zeros((54, 3), np.float64)
        obj[:, :2] = np.mgrid[0:9, 0:6].T.reshape(-1, 2)*.02
        rvec, tvec = np.array([.16, -.12, .03]), np.array([-.08, -.05, .85])
        K = np.array([[950., 0, 640.], [0, 955., 360.], [0, 0, 1.]])
        points = cv.projectPoints(obj, rvec, tvec, K, np.array(calibration["distortion"]))[0]
        expected = np.linalg.inv(transform(rotation(rvec), tvec))
        candidates = board_pose_candidates(points, calibration, REFERENCE)
        self.assertEqual({c["corner_order"] for c in candidates}, {"detected", "reversed"})
        best = min((c for c in candidates if c["corner_order"] == "detected"),
                   key=lambda c: c["reprojection_rms_px"])
        np.testing.assert_allclose(best["T_board_camera"], expected, atol=1e-7)
        self.assertGreater(best["min_corner_depth_m"], 0)
        self.assertLess(best["reprojection_rms_px"], 1e-6)
        reversed_candidates = board_pose_candidates(points[::-1], calibration, REFERENCE)
        recovered = min((c for c in reversed_candidates if c["corner_order"] == "reversed"),
                        key=lambda c: c["reprojection_rms_px"])
        np.testing.assert_allclose(recovered["T_board_camera"], expected, atol=1e-7)
        json.dumps(candidates, allow_nan=False)

    def test_nonfinite_corner_input_rejected_before_pnp(self):
        calibration = {"validated": True, "fx": 900., "fy": 900., "cx": 640., "cy": 360.,
                       "width": 1280, "height": 720, "distortion": [0.]*5}
        points = np.full((54, 2), 100.)
        points[0, 0] = np.nan
        with self.assertRaises(ValueError):
            board_pose_candidates(points, calibration, REFERENCE)

class SimilarityTests(unittest.TestCase):
    def assert_qualified(self, result):
        self.assertTrue(result["qualified"], result)
        self.assertEqual(result["status"], "qualified")
        self.assertEqual(result["failed_checks"], [])
        json.dumps(result, allow_nan=False)

    def test_exact_similarity_and_heldout_poses(self):
        values, scale, R, t = observations()
        result = fit(values)
        self.assert_qualified(result)
        self.assertAlmostEqual(result["scale_m_per_map_unit"], scale, places=9)
        np.testing.assert_allclose(result["R_board_map"], R, atol=1e-9)
        np.testing.assert_allclose(result["t_board_map_m"], t, atol=1e-9)
        self.assertEqual(result["validation_observation_ids"], ["o7", "o9"])
        self.assertNotIn("o7", result["training_observation_ids"])
        self.assertEqual(len(result["quality"]["anchors"]), 10)
        self.assertEqual(result["resolved_map_revision"], 20)

    def test_collinear_centers_get_rotation_from_camera_orientations(self):
        values, scale, R, t = observations(collinear=True)
        result = fit(values)
        self.assert_qualified(result)
        self.assertAlmostEqual(result["scale_m_per_map_unit"], scale, places=9)
        np.testing.assert_allclose(result["R_board_map"], R, atol=1e-9)

    def test_training_outlier_is_excluded_without_using_validation_to_refit(self):
        values, scale, _, _ = observations()
        T = np.array(values[4]["board_pose_candidates"][0]["T_board_camera"])
        T[:3, 3] += [.4, .3, 0]
        values[4]["board_pose_candidates"][0]["T_board_camera"] = T.tolist()
        result = fit(values)
        self.assert_qualified(result)
        self.assertIn("o4", result["quality"]["rejected_training_observation_ids"])
        self.assertNotIn("o4", result["training_observation_ids"])
        self.assertAlmostEqual(result["scale_m_per_map_unit"], scale, places=9)

    def test_projected_live_style_corner_candidates_feed_trajectory_fit(self):
        values, scale, _, _ = observations()
        calibration = {
            "validated": True, "lens_model": "opencv_pinhole",
            "fx": 950., "fy": 955., "cx": 640., "cy": 360.,
            "width": 1280, "height": 720, "distortion": [.01, -.01, .001, -.002, .005],
        }
        K = np.array([[950., 0, 640.], [0, 955., 360.], [0, 0, 1.]])
        obj = np.zeros((54, 3), np.float64)
        obj[:, :2] = np.mgrid[0:9, 0:6].T.reshape(-1, 2)*.02
        for i, item in enumerate(values):
            Tcb = np.linalg.inv(np.array(item["board_pose_candidates"][0]["T_board_camera"]))
            rvec = cv.Rodrigues(Tcb[:3, :3])[0]
            corners = cv.projectPoints(obj, rvec, Tcb[:3, 3], K,
                                       np.asarray(calibration["distortion"]))[0]
            if i % 2:
                corners = corners[::-1].copy()
            item["board_pose_candidates"] = board_pose_candidates(corners, calibration, item["reference"])
        result = fit(values)
        self.assert_qualified(result)
        self.assertAlmostEqual(result["scale_m_per_map_unit"], scale, delta=1e-5)
        self.assertLess(result["quality"]["validation_position_max_m"], 1e-5)

    def test_small_measurement_noise_is_reported_and_supported(self):
        values, scale, _, _ = observations()
        rng = np.random.default_rng(71)
        for item in values:
            T = np.array(item["board_pose_candidates"][0]["T_board_camera"])
            T[:3, 3] += rng.normal(0, .0005, 3)
            T[:3, :3] = rotation(rng.normal(0, .0005, 3)) @ T[:3, :3]
            item["board_pose_candidates"][0]["T_board_camera"] = T.tolist()
        result = fit(values)
        self.assert_qualified(result)
        self.assertAlmostEqual(result["scale_m_per_map_unit"], scale, delta=.02)
        self.assertGreater(result["quality"]["training_position_rms_m"], 0)

    def test_bad_heldout_view_rejects_good_training_fit(self):
        values, _, _, _ = observations()
        T = np.array(values[9]["board_pose_candidates"][0]["T_board_camera"])
        T[:3, 3] += [.25, 0, 0]
        values[9]["board_pose_candidates"][0]["T_board_camera"] = T.tolist()
        result = fit(values)
        self.assertFalse(result["qualified"])
        self.assertEqual(result["status"], "rejected")
        self.assertIn("held_out_pose_residual", result["failed_checks"])

    def test_alternating_corner_orders_use_one_consistent_board_frame(self):
        values, scale, _, _ = observations()
        symmetry = transform(np.diag([-1., -1., 1.]), [.16, .10, 0])
        for i, item in enumerate(values):
            original = item["board_pose_candidates"][0]
            alternative = copy.deepcopy(original)
            alternative["T_board_camera"] = (symmetry @ np.array(original["T_board_camera"])).tolist()
            alternative["corner_order"] = "reversed"
            item["board_pose_candidates"] = [original, alternative] if i % 2 == 0 else [alternative, original]
        result = fit(values)
        self.assert_qualified(result)
        self.assertAlmostEqual(result["scale_m_per_map_unit"], scale, places=9)
        self.assertEqual({r["candidate_index"] for r in result["quality"]["training_candidate_choices"]}, {0, 1})

    def test_competing_nonequivalent_scales_are_ambiguous(self):
        values, _, _, _ = observations()
        for item in values:
            other = copy.deepcopy(item["board_pose_candidates"][0])
            T = np.array(other["T_board_camera"])
            T[:3, 3] *= 2
            other["T_board_camera"] = T.tolist()
            other["pnp_branch"] = 1
            item["board_pose_candidates"].append(other)
        result = fit(values)
        self.assertFalse(result["qualified"])
        self.assertIn("ambiguous_pose_solution", result["failed_checks"])
        self.assertIsNone(result["scale_m_per_map_unit"])

    def test_moving_board_with_static_slam_camera_cannot_supply_scale(self):
        values, _, _, _ = observations()
        location = np.array(values[0]["T_map_camera"])[:3, 3]
        for item in values:
            T = np.array(item["T_map_camera"])
            T[:3, 3] = location
            item["T_map_camera"] = T.tolist()
        result = fit(values)
        self.assertFalse(result["qualified"])
        self.assertIn("insufficient_slam_translation", result["failed_checks"])
        self.assertIsNone(result["scale_m_per_map_unit"])

    def test_size_verification_is_only_remaining_failure_for_good_nominal_reference(self):
        values, _, _, _ = observations()
        for item in values:
            item["reference"]["size_verified"] = False
        result = fit(values)
        self.assertFalse(result["qualified"])
        self.assertEqual(result["status"], "provisional")
        self.assertEqual(result["failed_checks"], ["reference_size_unverified"])
        self.assertIsNotNone(result["scale_m_per_map_unit"])

    def test_empty_and_missing_holdout_are_provisional(self):
        empty = estimate_metric_attachment([], validation_sample_ids=[])
        self.assertEqual(empty["status"], "provisional")
        self.assertIsNone(empty["scale_m_per_map_unit"])
        self.assertIn("insufficient_training_observations", empty["failed_checks"])
        values, _, _, _ = observations()
        no_holdout = fit(values, validation=())
        self.assertEqual(no_holdout["status"], "provisional")
        self.assertEqual(no_holdout["failed_checks"], ["insufficient_validation_observations"])

    def test_identity_revision_stationarity_and_matrix_failures_are_explicit(self):
        mutations = [
            ("map_id_mismatch", lambda v: v[1].update(map_id="another-map")),
            ("calibration_id_mismatch", lambda v: v[1].update(calibration_id="another-calibration")),
            ("resolved_map_revision_mismatch", lambda v: v[1].update(resolved_map_revision=21)),
            ("reference_not_stationary", lambda v: v[1]["reference"].update(stationary=False)),
            ("duplicate_keyframe_id", lambda v: v[1].update(keyframe_id=v[0]["keyframe_id"])),
            ("invalid_map_camera_pose", lambda v: v[1]["T_map_camera"][0].__setitem__(0, float("nan"))),
        ]
        for reason, mutation in mutations:
            with self.subTest(reason=reason):
                values, _, _, _ = observations()
                mutation(values)
                result = fit(values)
                self.assertFalse(result["qualified"])
                self.assertIn(reason, result["failed_checks"])
                json.dumps(result, allow_nan=False)

    def test_insufficient_real_baseline_never_qualifies(self):
        values, _, _, _ = observations()
        for item in values:
            item["T_map_camera"][0][3] *= .1
            item["T_map_camera"][1][3] *= .1
            item["T_map_camera"][2][3] *= .1
            T = np.array(item["board_pose_candidates"][0]["T_board_camera"])
            T[:3, 3] = np.array([0., 0., -1.]) + .1*(T[:3, 3]-np.array([0., 0., -1.]))
            item["board_pose_candidates"][0]["T_board_camera"] = T.tolist()
        result = fit(values)
        self.assertFalse(result["qualified"])
        self.assertIn("insufficient_metric_translation", result["failed_checks"])

class DisplacementTests(unittest.TestCase):
    def endpoint(self, ident, x):
        base = observations()[0][0]
        for key, prefix in (("observation_id", "o"), ("frame_id", "f"), ("snapshot_id", "o"), ("keyframe_id", "k")):
            base[key] = prefix + str(ident)
        base["T_map_camera"] = transform(np.eye(3), [x, 0, 0]).tolist()
        return base

    def test_measured_three_metres_with_distinct_validation_endpoints(self):
        a, b, c, d = [self.endpoint(i, x) for i, x in enumerate((0., 1.5, 2., 3.))]
        result = estimate_displacement_scale(a, b, measured_distance_m=3.,
            validation_measurements=[{"start_observation": c, "end_observation": d, "measured_distance_m": 2.}])
        self.assertTrue(result["qualified"], result)
        self.assertEqual(result["scale_m_per_map_unit"], 2.)
        self.assertIsNone(result["R_board_map"])
        self.assertIsNone(result["t_board_map_m"])
        json.dumps(result, allow_nan=False)

    def test_unvalidated_distance_is_provisional_and_reused_endpoint_rejected(self):
        a, b, c = [self.endpoint(i, x) for i, x in enumerate((0., 1.5, 3.))]
        result = estimate_displacement_scale(a, b, measured_distance_m=3.)
        self.assertEqual(result["status"], "provisional")
        self.assertEqual(result["failed_checks"], ["insufficient_validation_measurements"])
        reused = estimate_displacement_scale(a, b, measured_distance_m=3.,
            validation_measurements=[{"start_observation": a, "end_observation": c, "measured_distance_m": 6.}])
        self.assertIn("validation_endpoints_not_independent", reused["failed_checks"])

    def test_bad_validation_distance_or_zero_map_motion_rejected(self):
        a, b, c, d = [self.endpoint(i, x) for i, x in enumerate((0., 1.5, 2., 3.))]
        bad = estimate_displacement_scale(a, b, measured_distance_m=3.,
            validation_measurements=[{"start_observation": c, "end_observation": d, "measured_distance_m": 3.}])
        self.assertIn("held_out_scale_error", bad["failed_checks"])
        b["T_map_camera"] = copy.deepcopy(a["T_map_camera"])
        zero = estimate_displacement_scale(a, b, measured_distance_m=3.)
        self.assertIn("insufficient_slam_translation", zero["failed_checks"])

class PolicyTests(unittest.TestCase):
    def test_policy_is_validated_without_mutating_defaults(self):
        changed = validate_policy({"min_baseline_m": .3})
        self.assertEqual(changed["min_baseline_m"], .3)
        self.assertEqual(DEFAULT_POLICY["min_baseline_m"], .15)
        for value in ({"unknown": 1}, {"min_baseline_m": float("nan")},
                      {"min_inlier_fraction": 1.1}, {"min_training_observations": 2},
                      {"min_validation_observations": True}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_policy(value)

if __name__ == "__main__":
    unittest.main()
