import unittest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vision_service import VisionService
from world_atlas import WorldAtlas


class SurveyWorldTest(unittest.TestCase):
    def test_survey_survives_pending_visual_registration_and_stale_radio(self):
        floor = dict(frame_id="enu:123", valid=True, elevation_m=0.)
        tof = dict(baseline=dict(valid=True, ground_plane=floor, positions=[]), status="grounded",
                   position=dict(x_m=1., y_m=2., z_m=.0635), ground_position=dict(x_m=1., y_m=2., z_m=0.),
                   valid=True, collector_age_s=1.)
        result = {"valid": False}
        VisionService._attach_survey(result, tof)
        atlas = object.__new__(WorldAtlas)
        atlas.registration_status = lambda *_: result
        mapped = atlas._world_registration("m", [("m",1,"[]",None,None)])
        self.assertFalse(mapped["valid"])
        self.assertEqual(mapped["surveyed_world"]["rover_ground_position"], tof["ground_position"])
        tof["collector_age_s"] = 9.
        VisionService._attach_survey(result, tof)
        self.assertIsNone(result["surveyed_world"]["rover_ground_position"])
        self.assertEqual(result["floor"], floor)
