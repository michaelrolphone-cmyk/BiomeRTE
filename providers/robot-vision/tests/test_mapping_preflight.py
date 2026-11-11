"""Preflight regressions for stale transport, identity changes and incomplete maps."""
import contextlib
import copy
import importlib.util
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "mapping_preflight", Path(__file__).resolve().parents[1] / "mapping_preflight.py")
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)
I = [[1,0,0,0], [0,1,0,0], [0,0,1,0], [0,0,0,1]]


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.config = {"camera": {"url": "rtsp://name:PRIVATE@camera/stream"},
                       "ahrs": {"enabled": True}, "radio": {"enabled": True}}
        self.meta = {"calibration": {"calibration_id": "lens", "validated": True}}
        self.status = {"availability": "available", "state": "running", "error": None,
                       "map_id": "map", "session_id": "session",
                       "calibration": {"calibration_id": "lens", "errors": []},
                       "ahrs": {"enabled": False}, "radio": {"enabled": True}}
        self.snap = {"snapshot_id": "observation", "frame_id": "frame",
                     "map_id": "map", "map_revision": 12, "session_id": "session",
                     "calibration_id": "lens",
                     "freshness": {"is_fresh": True, "is_current_session": True,
                                   "receive_decode_age_s": 0.1, "freshness_threshold_s": 2},
                     "frame": {"lighting": {"mean_luma_0_255": 70,
                                           "low_light_observed": False}},
                     "localization": {"valid": True, "T_map_camera": copy.deepcopy(I)}}

    def report(self, **kwargs):
        return preflight.assess(self.config, self.meta, self.status,
                                {"availability": "available", "snapshot": self.snap}, **kwargs)

    def test_visual_mapping_is_not_navigation(self):
        report = self.report()
        self.assertTrue(report["mapping_observation_ready"])
        self.assertFalse(report["navigation_available"])
        self.assertFalse(report["metric_camera_pose_available"])
        self.assertFalse(report["source_exposure_time_qualified"])

    def test_offline_config_cannot_claim_runtime(self):
        report = preflight.assess(self.config)
        self.assertFalse(report["mapping_observation_ready"])
        self.assertTrue(report["optional_ingestion"]["ahrs"]["configured"])
        self.assertFalse(report["optional_ingestion"]["ahrs"]["running"])

    def test_response_delivery_expires_an_otherwise_fresh_snapshot(self):
        self.assertFalse(self.report(delivery_s=3)["mapping_observation_ready"])

    def test_restart_and_map_switch_reject_old_observations(self):
        for key in ("session_id", "map_id"):
            with self.subTest(key=key):
                old = self.snap[key]
                self.snap[key] = "old"
                self.assertFalse(self.report()["mapping_observation_ready"])
                self.snap[key] = old

    def test_calibration_transition_rejects_snapshot(self):
        self.meta["calibration"]["calibration_id"] = "new-lens"
        self.assertFalse(self.report()["mapping_observation_ready"])

    def test_status_error_is_not_success(self):
        self.status["error"] = "processing_failed"
        self.assertFalse(self.report()["mapping_observation_ready"])

    def test_stale_and_negative_age_are_not_fresh(self):
        for age in (5, -1, None, True, float("nan"), float("inf")):
            with self.subTest(age=age):
                self.snap["freshness"]["receive_decode_age_s"] = age
                self.assertFalse(self.report()["mapping_observation_ready"])

    def test_numeric_one_is_not_a_validity_boolean(self):
        self.snap["localization"]["valid"] = 1
        self.assertFalse(self.report()["mapping_observation_ready"])

    def test_invalid_rotation_does_not_become_pose(self):
        for matrix in (None, [], [[1]*4]*4,
                       [[-1,0,0,0], [0,1,0,0], [0,0,1,0], [0,0,0,1]]):
            with self.subTest(matrix=matrix):
                self.snap["localization"]["T_map_camera"] = matrix
                self.assertFalse(self.report()["mapping_observation_ready"])

    def test_low_light_and_unknown_lighting_need_inspection(self):
        for lighting in ({}, {"low_light_observed": True, "mean_luma_0_255": 5}):
            self.snap["frame"]["lighting"] = lighting
            self.assertFalse(self.report()["mapping_observation_ready"])

    def test_relocated_grid_cannot_continue_previous_collection(self):
        self.status["metric_calibration"] = {
            "session": {"session_id": "old-scale", "state": "collecting"}}
        report = self.report(grid_relocated=True)
        self.assertEqual(report["metric_session_id"], "old-scale")
        self.assertIn("Cancel the prior collection", report["scale_actions"][1])
        self.assertIn("new checkerboard placement/session", report["scale_actions"][0])

    def test_metric_pose_requires_exact_snapshot_binding(self):
        self.snap["metric_localization"] = {
            **{k: self.snap[k] for k in ("snapshot_id", "frame_id", "map_id",
                                        "map_revision", "calibration_id")},
            "availability": "available", "valid": True, "units": "m",
            "T_map_camera": copy.deepcopy(I)}
        self.assertTrue(self.report()["metric_camera_pose_available"])
        self.snap["metric_localization"]["frame_id"] = "other-frame"
        self.assertFalse(self.report()["metric_camera_pose_available"])

    def test_report_does_not_change_evidence_or_expose_credentials(self):
        original = copy.deepcopy((self.config, self.meta, self.status, self.snap))
        report = self.report()
        self.assertEqual(original, (self.config, self.meta, self.status, self.snap))
        self.assertNotIn("PRIVATE", json.dumps(report))
        self.assertNotIn("rtsp://", json.dumps(report))

    def test_transport_rejects_mutations_before_connecting(self):
        with patch.object(preflight, "UnixHTTP") as connection:
            for operation in ("robot.camera.light.set", "robot.ahrs.calibrate",
                              "robot.map.scale.start", "robot.map.checkpoint"):
                with self.assertRaises(ValueError):
                    preflight.query("/does-not-exist", operation)
            connection.assert_not_called()

    def test_offline_cli_never_contacts_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "config.json").write_text(json.dumps(self.config))
            output = io.StringIO()
            with patch.object(preflight, "query") as query, contextlib.redirect_stdout(output):
                self.assertEqual(preflight.main(["--state-dir", directory, "--offline"]), 2)
            query.assert_not_called()
            report = json.loads(output.getvalue())
            self.assertFalse(report["mapping_observation_ready"])
            self.assertNotIn("PRIVATE", output.getvalue())

    def test_failed_socket_does_not_print_server_or_config_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "config.json").write_text(json.dumps(self.config))
            output = io.StringIO()
            with patch.object(preflight, "query", side_effect=OSError("PRIVATE")), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(preflight.main(["--state-dir", directory]), 2)
            self.assertNotIn("PRIVATE", output.getvalue())
            self.assertFalse(json.loads(output.getvalue())["motion_commands"])


if __name__ == "__main__":
    unittest.main()
