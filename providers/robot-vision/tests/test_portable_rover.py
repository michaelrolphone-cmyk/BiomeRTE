
import unittest,sys,copy
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from portable_rover import body_definition,sonar_snapshot,enrich
from ground_projection import project_ground

class Source:
    poll_interval_s=.26
    def status(self):return {"state":"connected"}
    def snapshot(self):
        return {"fresh":True,"received_at":"2026-11-12T06:00:00+00:00",
          "telemetry":{"boot_id":"boot-1","sequence":11,"ultrasonic":{"sensors":[
           {"id":"front","valid":True,"distance_m":.4,"sequence":5,"status":"ok"},
           {"id":"left","valid":True,"distance_m":.7,"sequence":8,"status":"ok"},
           {"id":"right","valid":False,"distance_m":None,"sequence":2,"status":"timeout"},
           {"id":"rear","valid":True,"distance_m":1.2,"sequence":10,"status":"ok"}]}},
          "observations":[{"id":"front","effective_age_ms":100},
            {"id":"left","effective_age_ms":1500},{"id":"right","effective_age_ms":None},
            {"id":"rear","effective_age_ms":400}]}

class PortableTest(unittest.TestCase):
    def service(self):
        model=body_definition()
        return SimpleNamespace(map_id="map-1", ultrasonic=Source(),
          _robot_model=lambda:{"availability":"available","definition":model},
          metric=SimpleNamespace(attachment_status=lambda *a,**k:
                 {"availability":"unavailable","reason":"metric_scale_not_applied"}))
    def test_body_units_and_fixed_mounts(self):
        model=body_definition(mounts={"left":{"origin_camera_m":[-.043,.0381,-.049],"direction_camera":[-1,0,0]}})
        self.assertEqual(model["geometry"]["wheelbase_m"],.127)
        self.assertAlmostEqual(model["geometry"]["track_width_m"],.1143)
        self.assertEqual(model["ahrs_mounting"]["position_offset_camera_m"],[0,0,-.004])
        self.assertAlmostEqual(model["sonar_poses"]["left"]["position_body_m"][1],.043)
        self.assertTrue(model["floor_contact"]["always_on_floor"])
        self.assertFalse(model["control"]["pwm_available"])
        self.assertFalse(model["control"]["speed_model"]["calibrated"])
    def test_sonar_freshness_is_per_reading(self):
        result=sonar_snapshot(self.service());s={x["id"]:x for x in result["sensors"]}
        self.assertTrue(s["front"]["fresh"]);self.assertFalse(s["left"]["fresh"])
        self.assertFalse(s["right"]["valid"]);self.assertIsNone(s["right"]["range_m"])
        self.assertEqual(s["rear"]["range_m"],1.2)
        self.assertAlmostEqual(result["update_rate"]["host_poll_hz"],1/.26)
    def test_no_fabricated_monocular_scale(self):
        result=enrich(self.service(),"robot.vision.snapshot",{"snapshot":{"map_id":"map-1","map_revision":4}})
        self.assertFalse(result["snapshot"]["metric_scale_available"])
        self.assertTrue(result["snapshot"]["local_metric_geometry_available"])
    def test_ground_query_needs_no_caller_assertion(self):
        result=project_ground([1,1],{},[640,480])
        self.assertEqual(result["reason"],"calibration_unavailable")
if __name__=="__main__":unittest.main()
