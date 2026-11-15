import copy,json,math,pathlib,struct,sys,tempfile,threading,time,unittest
from types import SimpleNamespace
import numpy as np
ROOT=pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/"providers/robot-vision"))
from stream_derivations import StreamDerivations,display_fit,range_fit
from sensor_rig_registry import configured_sensors,RigSampleFeed,manifest
from lidar_fusion import defaults
from dual_camera_feed import DualCameraFrames,RearCameraFrames
from world_snapshot_stream import snapshot_message

class ParityDerivations(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=pathlib.Path(self.temp.name);I=np.eye(4).tolist()
        model={"camera":{"T_robot_camera":I},"sonar_poses":{r:{"direction_body":[1,0,0],"position_body_m":[0,0,0]} for r in ("front","rear","left","right")}}
        self.service=SimpleNamespace(state_dir=self.path,_robot_model=lambda:{"definition":model},
            lidar=SimpleNamespace(lock=threading.RLock(),config=defaults(),path=self.path/"sensor-rig.json"),
            config={"camera":{"expected_fps":5}},
            camera_rig=SimpleNamespace(config={"rear":{"enabled":True,"camera":{"expected_fps":5},"T_front_camera_rear_camera_m":I}}))
        self.base={"world_id":"survey","reference":{"T_enu_body":I,"T_enu_camera":I,"T_enu_imu":I},"checkpoint":{}}
        (self.path/"survey-world.json").write_text(json.dumps(self.base))

    def test_enu_lidar_axial_depth_and_stable_observation(self):
        d=StreamDerivations(self.service);mount=defaults()["lidar"]["front"];mount["T_front_camera_sensor_m"]=np.eye(4).tolist()
        raw={"valid":True,"ready":True,"resolution":64,"observed_monotonic_us":100000,
             "sequence":1,"distance_mm":[1000]*64,"target_status":[5]*64,"age_ms":0}
        m={"lidar":{"latest":{"received_monotonic":time.monotonic(),"boot_id":"b","sensors":[{"sensor_id":"front","raw":raw,"mount":mount}]}}}
        d.enrich(m,b"jpeg");hits=m["lidar"]["world_hits"]
        self.assertEqual(len(hits),64);self.assertTrue(all(h["xyz_m"][2]==1 for h in hits))
        self.assertAlmostEqual(hits[0]["xyz_m"][0],.875*math.tan(math.pi/8))
        self.assertIn("mapping_available",m["lidar"]["latest"])
        self.assertIn("native_landmarks_refined",m["lidar"]["latest"])
        again=copy.deepcopy(m);d.enrich(again,b"")
        self.assertEqual(hits,again["lidar"]["world_hits"])
        self.assertEqual(m["world_orientation"]["pose"]["rotation"],again["world_orientation"]["pose"]["rotation"])

    def test_projection_matches_preserved_native_survey_landmarks(self):
        model=self.service._robot_model()["definition"]
        model["camera"]["T_robot_camera"]=[[0,0,1,.047625],[-1,0,0,0],[0,-1,0,.0635],[0,0,0,1]]
        base=copy.deepcopy(self.base)
        base["reference"]["T_enu_body"]=[[0,-1,0,.0381],[1,0,0,.638175],[0,0,1,0],[0,0,0,1]]
        (self.path/"survey-world.json").write_text(json.dumps(base))
        expected=[[.09997315087948108,.9248,-.003122411231273506],[.0892035982577806,1.0148,-.00167266376296768],[.05560052301026326,1.0238,-.004002615051316341]]
        mount=defaults()["lidar"]["front"]
        mount["T_front_camera_sensor_m"]=[[1,0,0,0],[0,1,0,-.02],[0,0,1,0],[0,0,0,1]]
        distances=[0]*64;statuses=[255]*64
        for zone,mm in [(1,239),(10,329),(11,338)]:distances[zone]=mm;statuses[zone]=5
        m={"lidar":{"latest":{"boot_id":"b","received_monotonic":time.monotonic(),"sensors":[{"sensor_id":"front","mount":mount,
            "raw":{"valid":True,"ready":True,"resolution":64,"observed_monotonic_us":1,"distance_mm":distances,"target_status":statuses,"age_ms":0}}]}}}
        StreamDerivations(self.service).enrich(m,b"")
        np.testing.assert_allclose([h["xyz_m"] for h in m["lidar"]["world_hits"]],expected,atol=1e-10)

    def test_fresh_body_pose_wins_stale_pose_cannot_jump(self):
        d=StreamDerivations(self.service);T=np.eye(4);T[0,3]=2
        m={"world_body_pose":{"valid":True,"T_enu_body":T.tolist(),"world_id":"survey","age_s":.1}}
        d.enrich(m,b"");self.assertEqual(m["world_orientation"]["source"],"world_body_pose")
        self.assertEqual(m["world_orientation"]["pose"]["x"],2)
        T[0,3]=999;m["world_body_pose"]["T_enu_body"]=T.tolist();m["world_body_pose"]["age_s"]=30
        d.enrich(m,b"");self.assertEqual(m["world_orientation"]["source"],"fallback")
        self.assertEqual(m["world_orientation"]["pose"]["x"],2)

    def test_accepted_inertial_pose_preserves_survey_zero_and_rejects_older_fix(self):
        d=StreamDerivations(self.service)
        accepted={"schema":"rover.world-body-pose/1","world_id":"survey","position_enu_m":[2,3,0],
                  "observed_at":20,"survey_zero_id":"zero","attitude_observed_at":20,
                  "rotation_enu_body":np.eye(3).tolist(),"source":"inertial"}
        m={"tof":{"collector_age_s":.1,"inertial_navigation":{"world_body_pose":accepted}}}
        d.enrich(m,b"");self.assertEqual(m["world_orientation"]["source"],"world_body_pose")
        self.assertEqual(m["world_orientation"]["pose"]["x"],2)
        accepted["observed_at"]=19;accepted["position_enu_m"]=[99,99,99]
        d.enrich(m,b"");self.assertEqual(m["world_orientation"]["pose"]["x"],2)

    def test_attitude_holds_latest_device_sample_without_renewing_age(self):
        d=StreamDerivations(self.service)
        def sample(uptime,yaw):
            return {"ahrs":{"receiver_id":"r","boot_id":"b","ahrs":{"attitude":{
                "valid":True,"roll_deg":0,"pitch_deg":0,"yaw_deg":yaw,
                "observed_uptime_ms":uptime,"age_ms":10}}}}
        a,age,_,_=d.attitude_observation(sample(20,90))
        observed=d.latest_attitude[2]
        a,_,_,_=d.attitude_observation(sample(10,0))
        self.assertEqual(a["yaw_deg"],90);self.assertEqual(d.latest_attitude[2],observed)
        a,_,_,_=d.attitude_observation({})
        self.assertEqual(a["yaw_deg"],90);self.assertEqual(d.latest_attitude[2],observed)

    def test_range_fit_matches_known_geometry_and_rejects_degenerate(self):
        points=[[0,0,0],[4,0,0],[0,4,0],[0,0,4],[4,4,4]];truth=np.array([1.,1.5,2.])
        anchors=[{"id":str(i),"point":p} for i,p in enumerate(points)]
        ranges=[{"target_bssid":str(i),"distance_m":float(np.linalg.norm(truth-p)),"age_s":.1} for i,p in enumerate(points)]
        result=range_fit(anchors,ranges);np.testing.assert_allclose(result["point"],truth,atol=1e-7)
        fit=display_fit({"collector_age_s":0,"baseline":{"positions":[dict(bssid=a["id"],x_m=a["point"][0],y_m=a["point"][1],z_m=a["point"][2]) for a in anchors]},"rover_ranges":ranges})
        self.assertEqual(fit["source"],"range_fit");np.testing.assert_allclose(fit["point"],truth,atol=1e-7)
        self.assertIsNone(range_fit(anchors[:3],ranges))
        self.assertEqual(display_fit({"collector_age_s":100})["source"],"unavailable")

    def test_rig_declares_offline_sensors_and_new_source_is_automatic(self):
        rows=configured_sensors(self.service);ids={r["id"] for r in rows}
        self.assertTrue({"camera.front","camera.rear","lidar.front","lidar.rear","imu.front","ahrs.front","tof.front"}<=ids)
        self.assertEqual(sum(r["type"]=="ultrasonic" for r in rows),4)
        first=manifest(self.service,{})
        self.assertTrue(all(r["last_seen"] is None for r in first["sensors"]))
        value=json.loads(self.service.lidar.path.read_text())
        value["revision"]+=1;value["sensors"].append({"id":"test.temperature","type":"temperature","role":"test","rate_hz":2,
            "source":{"kind":"state_file","path":"test-sensor.json"}})
        (self.path/"test-sensor.json").write_text('{"celsius":21}')
        self.service.lidar.path.write_text(json.dumps(value))
        feed=RigSampleFeed(self.service)
        samples=feed.collect(now=1)
        self.assertEqual(samples[0]["id"],"test.temperature")
        self.assertEqual(samples[0]["value"],{"celsius":21})
        self.assertEqual(feed.collect(now=1.1),[])
        self.assertEqual(feed.collect(now=1.6)[0]["id"],"test.temperature")
        after=manifest(self.service,{})
        self.assertNotEqual(first["revision"],after["revision"])
        self.assertIn("test.temperature",{r["id"] for r in after["sensors"]})

    def test_fanout_manifest_then_rear_to_each_subscriber(self):
        shared=DualCameraFrames();rear=RearCameraFrames(shared)
        shared.publish_manifest({"type":"sensor_manifest","revision":"r","sensors":[]})
        with shared.subscribe() as a,shared.subscribe() as b:
            for box in (a,b):
                data=box.get(timeout=.1);size=struct.unpack("!I",data[4:8])[0]
                self.assertEqual(json.loads(data[8:8+size])["type"],"sensor_manifest")
            rear.publish({"frame_id":"rear1","processed_monotonic":time.monotonic()},b"jpeg")
            self.assertEqual(a.get(timeout=.1),b.get(timeout=.1))

    def test_world_bootstrap_preserves_future_metadata_and_completion(self):
        data={"map":{"map_id":"w","revision":8,"metadata":{"attitude_world_reference":{"schema":"rover.attitude.world-reference/1"},"future_sensor":{"value":42}}},"entities":[],"next_cursor":None}
        message=snapshot_message(lambda *args:copy.deepcopy(data))
        self.assertIs(message["complete"],True)
        self.assertEqual(message["data"]["map"]["metadata"],data["map"]["metadata"])


class RigTopology(unittest.TestCase):
    setUp=ParityDerivations.setUp
    def test_native_lidar_addition_and_removal_updates_manifest_without_inventory_edits(self):
        configured_sensors(self.service)
        rig=json.loads(self.service.lidar.path.read_text())
        rig["lidar"]["test"]=copy.deepcopy(rig["lidar"]["front"])
        self.service.lidar.path.write_text(json.dumps(rig))
        self.assertIn("lidar.test",{row["id"] for row in manifest(self.service,{})["sensors"]})
        rig=json.loads(self.service.lidar.path.read_text());del rig["lidar"]["test"]
        self.service.lidar.path.write_text(json.dumps(rig))
        self.assertNotIn("lidar.test",{row["id"] for row in manifest(self.service,{})["sensors"]})
