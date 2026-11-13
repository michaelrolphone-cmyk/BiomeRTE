"""The socket matrix must survive telemetry compaction, not require a poll."""
import copy,json,pathlib,sys,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from stream_telemetry import bounded_metadata,size

class ToFMatrixStreamTests(unittest.TestCase):
    def payload(self):
        ids=["14:63:93:91:86:89","14:63:93:91:a4:31","14:63:93:94:0f:29",
             "14:63:93:94:0f:e1","14:63:93:91:86:e1"]
        anchors=[]
        for i,key in enumerate(ids):
            anchors.append({"id":key,"bssid":key,"connected":True,"received_age_s":.2,
                "boot_id":123+i,"tx_power_dbm":8.5 if i==3 else 15,
                "height_known":False,"diagnostic":"x"*6000,
                "ranges":[{"target_bssid":other,"status":"ok","range_seq":4,
                    "age_ms":1000,"rtt_ns":30,"samples":12,"valid":True,"distance_m":4.5,
                    "ssid":"repeated discovery name"*30}
                    for other in ids if other!=key]})
        return {"type":"frame","frame_id":"frame:matrix","detections":[],
            "tof":{"schema":"rover.tof.solution/1","collector_age_s":.1,
                "valid":False,"status":"inconsistent_3d_ranges","anchors":anchors,
                "anchor_count":5,"edges":[]},
            "ahrs":{"ahrs":{"imu":{"accel_m_s2":[1,2,3],"valid":True}},
                "snapshot":{"telemetry":{"diagnostic":"z"*16000},
                            "received_monotonic":12,"elapsed_since_receive_s":.1}},
            "ultrasonic":{"schema":"hcsr04.v1","sensors":{"front":{"distance_m":.5,"valid":True}}}}
    def test_compaction_keeps_all_twenty_directed_measurements_and_clocks(self):
        original=self.payload();before=copy.deepcopy(original)
        value=bounded_metadata(original)
        self.assertLessEqual(size(value),15872)
        self.assertEqual(original,before)
        self.assertTrue(value["tof"]["anchor_range_details_compacted"])
        self.assertNotIn("anchor_range_details_omitted",value["tof"])
        self.assertEqual(len(value["tof"]["anchors"]),5)
        for a,b in zip(value["tof"]["anchors"],original["tof"]["anchors"]):
            self.assertEqual(a["tx_power_dbm"],b["tx_power_dbm"])
            self.assertEqual(a["received_age_s"],b["received_age_s"])
            self.assertEqual(a["boot_id"],b["boot_id"])
            self.assertEqual(len(a["ranges"]),4)
            for r,s in zip(a["ranges"],b["ranges"]):
                for k in ["target_bssid","status","range_seq","age_ms","rtt_ns","samples","valid","distance_m"]:
                    self.assertEqual(r[k],s[k])
                self.assertNotIn("ssid",r)
        self.assertEqual(value["ahrs"]["ahrs"]["imu"]["accel_m_s2"],[1,2,3])
        self.assertEqual(value["ultrasonic"]["sensors"]["front"]["distance_m"],.5)
    def test_failed_and_one_way_reports_remain_explicit(self):
        value=self.payload();report=value["tof"]["anchors"][0]["ranges"][0]
        report.update(valid=False,status="ftm_failed",samples=0,rtt_ns=0)
        wire=bounded_metadata(value)["tof"]["anchors"][0]["ranges"][0]
        self.assertFalse(wire["valid"]);self.assertEqual(wire["status"],"ftm_failed")
        self.assertEqual(wire["samples"],0);self.assertEqual(wire["age_ms"],1000)
    def test_small_metadata_is_not_unnecessarily_rewritten(self):
        value=self.payload()
        for a in value["tof"]["anchors"]:
            del a["diagnostic"]
            for r in a["ranges"]:r.pop("ssid")
        value["ahrs"].pop("snapshot")
        self.assertLess(size(value),15872)
        self.assertEqual(bounded_metadata(value),value)
    def test_real_five_node_collector_remains_under_wire_budget(self):
        path=pathlib.Path.home()/".local/share/biome-tof/latest.json"
        if not path.exists():self.skipTest("Live qualification only on deployed Mac")
        data=json.loads(path.read_text())
        value=self.payload();value["tof"]=data
        # Force the real metadata down the compaction branch.
        value["ahrs"]["source"]={"state":"available","diagnostics":"q"*30000}
        packed=bounded_metadata(value)
        self.assertLessEqual(size(packed),15872)
        self.assertEqual(sum(len(a.get("ranges",[])) for a in packed["tof"]["anchors"]),
                         sum(len(a.get("ranges",[])) for a in data["anchors"]))

    def test_baseline_state_and_coordinates_survive_with_directional_ranges(self):
        value=self.payload()
        positions=[{"bssid":a["bssid"],"x_m":i*2.,"y_m":i*.7,"z_m":i*.3,"model_sigma_m":.4} for i,a in enumerate(value["tof"]["anchors"])]
        value["tof"]["anchor_geometry"]=positions
        value["tof"]["baseline"]={"schema":"rover.tof.baseline/1","status":"recalibrating",
            "valid":False,"epoch":4,"frame_id":"tof-relative-1","positions":copy.deepcopy(positions),
            "movement_anchors":[positions[2]["bssid"]]}
        value["tof"]["edges"]=[{"diagnostic":"q"*20000}]
        value["ahrs"]["source"]={"state":"available","diagnostic":"z"*20000}
        original=copy.deepcopy(value);packed=bounded_metadata(value)
        self.assertLessEqual(size(packed),15872);self.assertEqual(value,original)
        baseline=packed["tof"]["baseline"]
        self.assertEqual(baseline["status"],"recalibrating");self.assertFalse(baseline["valid"])
        self.assertEqual(baseline["epoch"],4);self.assertEqual(baseline["frame_id"],"tof-relative-1")
        self.assertEqual(baseline["movement_anchors"],[positions[2]["bssid"]])
        self.assertEqual(baseline["positions_reference"],"anchor_geometry")
        self.assertEqual(packed["tof"]["anchor_geometry"],positions)
        self.assertEqual(sum(len(a["ranges"]) for a in packed["tof"]["anchors"]),20)


    def test_refinement_revision_and_progress_survive_header_pressure(self):
        value=self.payload();positions=[{"bssid":a["bssid"],"x_m":i,"y_m":i*.2,"z_m":i*.3} for i,a in enumerate(value["tof"]["anchors"])]
        value["tof"]["anchor_geometry"]=positions
        value["tof"]["request_age_s"]={"diagnostics":"x"*20000}
        value["tof"]["baseline"]={"epoch":2,"revision":8,"frame_id":"tof-relative-2",
            "positions":positions,"status":"ready","valid":True,
            "refinement":{"revision":8,"accepted":7,"state":"refined","last_update_at":1000,
                          "before_rms_m":.9,"after_rms_m":.8,"extra_diagnostics":"x"*20000}}
        packed=bounded_metadata(value);baseline=packed["tof"]["baseline"]
        self.assertLessEqual(size(packed),15872)
        self.assertEqual(baseline["revision"],8);self.assertEqual(baseline["epoch"],2)
        self.assertEqual(baseline["refinement"]["accepted"],7)
        self.assertEqual(baseline["refinement"]["after_rms_m"],.8)
        self.assertEqual(packed["tof"]["anchor_geometry"],positions)
        self.assertEqual(sum(len(a["ranges"]) for a in packed["tof"]["anchors"]),20)

if __name__=="__main__":unittest.main()
