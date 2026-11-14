import json,struct,unittest
from biomerte.stream_router import Session,decode,frame
from biomerte.store import Fault
class UnlimitedTelemetryTests(unittest.TestCase):
 def test_complete_large_header_and_payload_roundtrip(self):
  original={"type":"frame","frame_id":"frame:large","authority":"a","generation":1,"epoch":"e","sequence":1,"source_timestamp":None,"processing_timestamp":None,"calibration_revision":"cal","map_revision":1,"ahrs":{"samples":[{"original_clock_us":i,"accel":[1,2,3]} for i in range(32)]},"tof":{"forward":[1,2,3],"reverse":[3,2,1]},"diagnostics":"x"*5000000}
  wire=frame(original,b"jpeg")
  session=Session({"contract":"robot.vision.stream:1","authority":"a","generation":1,"epoch":"e","limits":{"max_message_bytes":2097152},"lanes":["vision"],"direction":"provider_to_server"})
  metadata,payload=session.check("provider","vision",wire)
  self.assertEqual(metadata,original);self.assertEqual(payload,b"jpeg")
 def test_truncated_header_is_rejected(self):
  with self.assertRaises(Fault):decode(struct.pack("!I",1000000)+b"{}")
 def test_nontelemetry_grant_limit_still_applies(self):
  with self.assertRaises(Fault):decode(json.dumps({"data":"x"*20}),8)

class StreamRecoveryTests(unittest.TestCase):
 def test_requeued_lease_without_guard_is_authoritative_conflict(self):
  from biomerte.streams import valid
  from types import SimpleNamespace
  lease={"id":"lease","state":"queued"}
  class Tx:
   def get(self,table,key):return lease if table=="leases" else None
  grant={"lease":"lease","state":"open","expires_at":9999999999,"authority":"node","realm":"site","generation":1}
  with self.assertRaises(Fault) as caught:valid(Tx(),SimpleNamespace(app=SimpleNamespace(node="node"),scope="site"),grant)
  self.assertEqual(caught.exception.status,409)

if __name__=="__main__":unittest.main()
