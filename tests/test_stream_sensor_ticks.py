import unittest
from types import SimpleNamespace
from biomerte.stream_router import Session,frame
from biomerte.store import Fault
class SensorContractTests(unittest.TestCase):
 def state(self):
  grant=dict(id="test",authority="a",generation=1,epoch=1,contract="robot.vision.stream:1",lanes=["vision"],direction="provider_to_server",limits={})
  return SimpleNamespace(grant=grant,sequences={},control_clock=None)
 def metadata(self):
  return dict(type="sensor_tick",authority="a",generation=1,epoch=1,sequence=1,frame_id="boot:1",image_available=False,sensor_packet=dict(schema="biome.sensor_tick.v1",receiver_id="device",boot_id="boot",capture_monotonic_us=1))
 def test_authenticated_sensor_tick_needs_no_image_or_slam_timestamp(self):
  meta=self.metadata()
  result,payload=Session.check(self.state(),"provider","vision",frame(meta,b""))
  self.assertEqual(result["sensor_packet"]["receiver_id"],"device")
  self.assertEqual(payload,b"")
 def test_sensor_tick_cannot_smuggle_an_image(self):
  with self.assertRaises(Fault):Session.check(self.state(),"provider","vision",frame(self.metadata(),b"jpeg"))
 def test_image_metadata_requirements_are_preserved(self):
  meta=self.metadata();meta.update(type="frame",source_timestamp="now")
  with self.assertRaises(Fault):Session.check(self.state(),"provider","vision",frame(meta,b"jpeg"))
 def test_missing_device_identity_is_rejected(self):
  meta=self.metadata();del meta["sensor_packet"]["receiver_id"]
  with self.assertRaises(Fault):Session.check(self.state(),"provider","vision",frame(meta,b""))
