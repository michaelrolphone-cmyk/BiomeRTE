import math
import unittest

from ground_projection import calibration_errors, project_ground


def calibration():
    return {
        "calibration_id": "test-camera", "width": 640, "height": 480,
        "fx": 600.0, "fy": 600.0, "cx": 320.0, "cy": 240.0,
        "distortion": [0, 0, 0, 0, 0], "validated": True,
        "provenance": "synthetic_known_camera",
        "mounting": {"height_m": .15, "pitch_down_deg": 10.0, "roll_deg": 0,
                     "provenance": "test", "validated": True},
    }


class GroundProjectionTests(unittest.TestCase):
    def test_positive_downward_pitch_has_correct_sign(self):
        result = project_ground([320, 240], calibration(), [640, 480], confirmed_floor_contact=True)
        self.assertEqual(result["availability"], "available")
        self.assertAlmostEqual(result["position"][0], .8506922729426564)
        self.assertAlmostEqual(result["position"][1], 0)
        self.assertEqual(result["coordinate_frame"], "camera_ground")

    def test_level_camera_horizon_and_ground(self):
        c = calibration()
        c["mounting"].update(height_m=.0635, pitch_down_deg=0)
        self.assertEqual(project_ground([320, 240], c, [640, 480], confirmed_floor_contact=True)["availability"], "unavailable")
        p = project_ground([380, 360], c, [640, 480], confirmed_floor_contact=True)
        self.assertAlmostEqual(p["position"][0], .3175)
        self.assertAlmostEqual(p["position"][1], -.03175)
        self.assertIsNone(p["global_map_position"])

    def test_unconfirmed_contact_is_never_projected(self):
        self.assertEqual(project_ground([320, 400], calibration(), [640, 480])["reason"], "floor_contact_not_confirmed")
        self.assertEqual(project_ground([320, 400], calibration(), [640, 480], confirmed_floor_contact=1)["reason"], "floor_contact_not_confirmed")

    def test_unknown_or_mismatched_calibration_is_unavailable(self):
        c = calibration()
        c["validated"] = False
        self.assertIn("intrinsics_not_validated", calibration_errors(c))
        c = calibration()
        c["distortion"] = None
        self.assertIn("distortion_missing_or_invalid", calibration_errors(c))
        r = project_ground([320, 400], calibration(), [1280, 720], confirmed_floor_contact=True)
        self.assertIn("calibration_resolution_mismatch", r["details"])

    def test_nonfinite_inputs_are_rejected(self):
        for key in ("fx", "fy", "cx", "cy"):
            c = calibration()
            c[key] = math.nan
            self.assertTrue(calibration_errors(c))
        self.assertEqual(project_ground([math.inf, 200], calibration(), [640, 480],
                                       confirmed_floor_contact=True)["reason"], "invalid_pixel")

    def test_lateral_sign_and_sensitivity(self):
        p = project_ground([380, 400], calibration(), [640, 480], confirmed_floor_contact=True)
        self.assertLess(p["position"][1], 0)
        self.assertIsNotNone(p["pixel_sensitivity"]["d_position_dv_m_per_px"])
        self.assertIsNone(p["covariance"])


if __name__ == "__main__":
    unittest.main()
