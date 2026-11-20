import json,re,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class LiveCaptureContractTest(unittest.TestCase):
 def test_manifest_entries_have_published_schemas_and_were_seen_on_agent_lease(self):
  proof=json.loads((ROOT/"contract/rover-live-capture.json").read_text())
  catalog=json.loads((ROOT/"rover-stream-schemas.json").read_text())
  schemas=catalog.get("schemas",catalog)
  capture=proof["captures"][0]
  self.assertEqual(capture["first"],{"vision":"sensor_manifest","map":"world_snapshot"})
  self.assertFalse(capture["missing_sensor_ids"])
  for sensor in capture["manifest"]["sensors"]:
   self.assertIn(sensor["schema"],schemas)
   self.assertIn(sensor["id"],capture["seen_sensor_ids"])
 def test_new_rig_sensor_crossed_wire_and_cleanup_was_published(self):
  proof=json.loads((ROOT/"contract/rover-live-capture.json").read_text())
  current=proof["captures"][-1]
  self.assertTrue(all(current["rig_experiment"].values()))
  self.assertFalse(current["schema_errors"])
  self.assertFalse(current["address_violations"])
  for field in ["world_orientation","tof.display_fit","lidar.world_hits","lidar.latest.mapping_available","lidar.latest.native_landmarks_refined"]:
   self.assertGreater(current["derived_fields"][field],0)
 def test_capture_proof_has_no_network_literals(self):
  text=(ROOT/"contract/rover-live-capture.json").read_text()
  self.assertNotRegex(text,r"https?://|(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")
if __name__=="__main__":unittest.main()
