import copy,json,pathlib,struct,sys,unittest
sys.path[:0]=[str(pathlib.Path(__file__).resolve().parents[1]),str(pathlib.Path(__file__).resolve().parents[3])]
from leased_publish import packet
from stream_telemetry import bounded_metadata
from biomerte.stream_router import decode
class WireIntegrityTests(unittest.TestCase):
    def payload(self):
        firmware={"schema":1,"ahrs":{"worker_ready":True,"paused":False,"imu":{"accel_m_s2":[1,2,3]}},
                  "wifi":{"aps":[{"ssid":"network"+str(i),"bssid":str(i),"rssi_dbm":-50,"age_ms":10} for i in range(60)]},
                  "ble":{"devices":[{"address":str(i),"rssi_dbm":-60,"age_ms":12} for i in range(60)]}}
        return {"frame_id":"bound","detections":[],"pose":{"valid":False},
                "ahrs":{"ahrs":firmware["ahrs"],"snapshot":{"telemetry":copy.deepcopy(firmware)}},
                "radio":{"enabled":True,"telemetry":{"fresh":True,"telemetry":firmware,
                    "observations":[{"radio":"wifi","index":i,"source_observation_id":"x"*150} for i in range(60)]}}}
    def test_large_cache_round_trips_without_header_truncation(self):
        original=self.payload();before=copy.deepcopy(original)
        self.assertGreater(len(json.dumps(original)),16384)
        value=packet(original,b"jpeg")
        m,image=decode(value[4:],4194304)
        self.assertGreater(struct.unpack("!I",value[4:8])[0],15872)
        self.assertEqual(m["radio"],original["radio"])
        self.assertTrue(m["ahrs"]["ahrs"]["worker_ready"])
        self.assertEqual(m["radio"]["telemetry"]["telemetry"]["wifi"]["aps"][0]["rssi_dbm"],-50)
        self.assertEqual(m["radio"]["telemetry"]["telemetry"]["ble"]["devices"][0]["rssi_dbm"],-60)
        self.assertEqual(image,b"jpeg")
        self.assertEqual(original,before)
    def test_requested_legacy_budget_does_not_omit_supplied_measurements(self):
        value=self.payload();fw=value["radio"]["telemetry"]["telemetry"]
        fw["wifi"]["aps"]*=20;fw["ble"]["devices"]*=20
        reduced=bounded_metadata(value,8000)
        self.assertGreater(len(json.dumps(reduced,separators=(",",":")).encode()),8000)
        self.assertEqual(reduced["radio"],value["radio"])
        self.assertNotIn("stream_scan_rows_omitted",reduced["radio"]["telemetry"])
        self.assertEqual(reduced["ahrs"]["ahrs"]["imu"]["accel_m_s2"],[1,2,3])
if __name__=="__main__":unittest.main()
