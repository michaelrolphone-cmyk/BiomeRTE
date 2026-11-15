import unittest
from types import SimpleNamespace
from live_camera import LiveCamera
class LiveTimestampTests(unittest.TestCase):
 def test_raw_frame_has_honest_transport_timestamp_without_claiming_slam(self):
  published=[]
  output=SimpleNamespace(publish=lambda metadata,image:published.append((metadata,image)))
  live=LiveCamera(None,output,None)
  capture=SimpleNamespace(sequence=1,metadata={"frame_id":"real"})
  meta=dict(frame_id="real",source_timestamp="captured",calibration_revision=2,map_revision=3,analysis_reference={"state":"pending"})
  live._publish(capture,meta,b"jpeg")
  value=published[0][0]
  self.assertIn("processing_timestamp",value)
  self.assertEqual(value["processing_stage"],"live_capture_publication")
  self.assertEqual(value["analysis_reference"]["state"],"pending")
  self.assertNotIn("processing_timestamp",meta)
