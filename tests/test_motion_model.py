import copy
import json
import math
import pathlib
import tempfile
import time
import unittest
from biomerte.motion_model import MotionModel, controls
from biomerte.motion_tracking import MotionRecorder, metric_delta

class MotionTests(unittest.TestCase):
    def test_neutral_retains_transient_memory_and_straight_relaxation_is_gradual(self):
        m=MotionModel()
        m.advance(m.state,5,1.)
        turned=list(m.state["steering_memory"])
        m.advance(m.state,0,4.)
        self.assertEqual(turned,m.state["steering_memory"])
        m.advance(m.state,1,.1)
        self.assertTrue(all(0<z<old for z,old in zip(m.state["steering_memory"],turned)))

    def test_prediction_uses_history_without_mutating_live_state(self):
        m=MotionModel()
        # Observed conditional turn response with a known one-pulse memory effect.
        for i in range(400):
            mask=[5,0,1,9,0,1][i%6]
            x=m.features(m.state,mask,.1)
            d,s=controls(mask)
            omega=12.*d*m.state["steering_memory"][1]+3.*d*s
            m.observe(mask,.1,omega*.1, .02*d)
        baseline=m.export()
        left=dict(copy.deepcopy(m.state),steering_memory=[.8]*4,steering_known=True)
        right=dict(copy.deepcopy(m.state),steering_memory=[-.8]*4,steering_known=True)
        a=m.predict([dict(mask=1,duration_s=.3)],left)["steps"][-1]["yaw_deg"]
        b=m.predict([dict(mask=1,duration_s=.3)],right)["steps"][-1]["yaw_deg"]
        self.assertGreater(a,b)
        self.assertEqual(baseline,m.export())

    def test_untrained_axes_are_null(self):
        m=MotionModel()
        out=m.predict([dict(mask=5,duration_s=.5)])
        self.assertIsNone(out["steps"][0]["distance_m"])
        self.assertIsNone(out["steps"][0]["yaw_deg"])
        self.assertFalse(out["executes_motion"])
        m.observe(5,.2,1.)
        self.assertTrue(m.predict([dict(mask=5,duration_s=.2)])["angular_available"])
        self.assertFalse(m.predict([dict(mask=5,duration_s=.2)])["metric_available"])

    def test_restart_keeps_memory_but_marks_motion_unknown(self):
        m=MotionModel();m.advance(m.state,5,1.)
        n=MotionModel(m.export())
        self.assertEqual(m.state["steering_memory"],n.state["steering_memory"])
        self.assertFalse(n.state["steering_known"])
        self.assertIsNone(n.state["speed_m_s"])

    def test_forgetting_can_follow_changed_response(self):
        m=MotionModel()
        for i in range(1200):
            m.observe(5,.2,1.)
        before=m.angular.predict(m.features(m.state,5,.2))[0]
        for i in range(150):
            m.observe(5,.2,2.)
        after=m.angular.predict(m.features(m.state,5,.2))[0]
        self.assertGreater(after,before+2.)
        self.assertGreater(m.angular.drift_events,0)

    def test_metric_rejects_turns_reused_samples_and_inconsistent_beams(self):
        a=dict(yaw=0,ranges={"front":dict(distance=1.,sequence=1),"rear":dict(distance=.5,sequence=1)})
        b=dict(yaw=0,ranges={"front":dict(distance=.9,sequence=2),"rear":dict(distance=.6,sequence=2)})
        self.assertAlmostEqual(metric_delta(a,b)[0],.1)
        b["ranges"]["rear"]["distance"]=.3
        self.assertIsNone(metric_delta(a,b)[0])
        b["yaw"]=8.
        self.assertIsNone(metric_delta(a,b)[0])

    def test_persistence_and_ambiguous_transition_not_trained(self):
        with tempfile.TemporaryDirectory() as directory:
            p=pathlib.Path(directory)/"motion.sqlite"
            r=MotionRecorder(p)
            now=time.monotonic()
            def frame(t,seq,yaw):
                return dict(frame=dict(capture_host_monotonic_interval_s=[t-.005,t+.005],
                    frame_id=str(seq),capture_boot_id="camera1"),
                    spatial_evidence=dict(ahrs=dict(data=dict(attitude=dict(yaw_deg=yaw,valid=True,age_ms=0)))))
            ack=dict(stage="applied",sequence=1,commanded_mask=5,authority="a",
                epoch="e",generation=1,host_issue_age_s=.05,provider_monotonic_ms=1000)
            r.offer("control",ack,now)
            r.offer("vision",frame(now+.1,1,0),now+.12)
            r.offer("vision",frame(now+.2,2,1),now+.22)
            # This transition can overlap the next frame; it cannot train.
            ack=dict(ack,sequence=2,commanded_mask=0,host_issue_age_s=.2)
            r.offer("control",ack,now+.25)
            r.offer("vision",frame(now+.3,3,2),now+.32)
            r.close()
            self.assertIsNone(r.error)
            self.assertEqual(r.snapshot["angular_samples"],1)
            self.assertGreater(r.snapshot["rejected"]["actuation_window_ambiguous"],0)
            r2=MotionRecorder(p);r2.offer("gap",dict(reason="test"));r2.close()
            self.assertEqual(r2.snapshot["angular_samples"],1)
            self.assertFalse(r2.snapshot["state"]["steering_known"])

    def test_validation(self):
        m=MotionModel()
        for pulses in [[],[dict(mask=3,duration_s=1)],[dict(mask=1,duration_s=float("nan"))]]:
            with self.assertRaises(ValueError):m.predict(pulses)

try:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
except ImportError:
    web=None

@unittest.skipIf(web is None,"aiohttp unavailable")
class EndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_endpoints_are_authenticated_and_read_only(self):
        from biomerte.motion_tracking import install_motion
        class Rover:
            control_ready=False
            motion_calls=0
            def broadcast(self,lane,message):pass
            def desired(self,mask,*args,**kwargs):
                self.motion_calls+=1
                return dict(accepted_sequence=1)
        rover=Rover()
        with tempfile.TemporaryDirectory() as directory:
            @web.middleware
            async def authenticate(request,handler):
                if request.headers.get("Authorization")!="Bearer test":
                    raise web.HTTPUnauthorized()
                return await handler(request)
            app=web.Application(middlewares=[authenticate])
            recorder=install_motion(rover,app,directory)
            client=TestClient(TestServer(app));await client.start_server()
            try:
                response=await client.get("/api/motion/model")
                self.assertEqual(response.status,401)
                recorder.offer("gap",dict(reason="test"))
                # Initialization is independent of the HTTP event loop.
                for unused in range(100):
                    if recorder.latest_model is not None:break
                    await __import__("asyncio").sleep(.01)
                response=await client.post("/api/motion/predict",
                    headers={"Authorization":"Bearer test"},
                    json={"pulses":[{"mask":5,"duration_s":.8},{"mask":0,"duration_s":.3},{"mask":1,"duration_s":.7}]})
                self.assertEqual(response.status,200)
                result=await response.json()
                self.assertEqual(len(result["steps"]),3)
                self.assertFalse(result["executes_motion"])
                self.assertIsNone(result["steps"][-1]["distance_m"])
                self.assertEqual(rover.motion_calls,0)
                response=await client.post("/api/motion/predict",headers={"Authorization":"Bearer test"},
                    json={"pulses":[{"mask":3,"duration_s":1}]})
                self.assertEqual(response.status,400)
            finally:await client.close()

class SonarAssociationTests(unittest.TestCase):
    def test_same_boot_device_times_produce_metric_training(self):
        with tempfile.TemporaryDirectory() as directory:
            r=MotionRecorder(pathlib.Path(directory)/"motion.sqlite")
            now=time.monotonic()
            ack=dict(stage="applied",sequence=1,commanded_mask=1,
                authority="a",epoch="e",generation=1,host_issue_age_s=.05)
            r.offer("control",ack,now)
            def sonar(at,seq,front,rear):
                return dict(boot_id="camera1",update_rate=dict(nominal_per_sensor_period_s=.26),
                    source_received_at="test",sensors=[
                        dict(id="front",valid=True,status="ok",range_m=front,sequence=seq,observed_uptime_ms=at),
                        dict(id="rear",valid=True,status="ok",range_m=rear,sequence=seq,observed_uptime_ms=at)])
            def frame(t,us,seq):
                return dict(frame=dict(capture_host_monotonic_interval_s=[t-.005,t+.005],
                    capture_monotonic_us=us,frame_id=str(seq),capture_boot_id="camera1"),
                    spatial_evidence=dict(ahrs=dict(sensors=dict(attitude=dict(yaw_deg=0,valid=True,age_ms=5)))))
            r.offer("sonar",sonar(1450,1,1.,.5),now+.05)
            r.offer("vision",frame(now+.1,1500000,1),now+.12)
            r.offer("sonar",sonar(1650,2,.9,.6),now+.25)
            r.offer("vision",frame(now+.3,1700000,2),now+.32)
            r.close()
            self.assertIsNone(r.error)
            self.assertEqual(r.snapshot["metric_samples"],1)
            self.assertAlmostEqual(r.snapshot["last_observation"]["distance_m"],.1)

    def test_attitude_reference_reset_does_not_train_a_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            r=MotionRecorder(pathlib.Path(directory)/"motion.sqlite")
            now=time.monotonic()
            r.offer("control",dict(stage="applied",sequence=1,commanded_mask=5,
                authority="a",epoch="e",generation=1,host_issue_age_s=.05),now)
            for i,generation in enumerate([1,2]):
                t=now+.1+i*.1
                r.offer("vision",dict(frame=dict(capture_host_monotonic_interval_s=[t-.005,t+.005],
                    frame_id=str(i),capture_boot_id="camera1"),
                    spatial_evidence=dict(ahrs=dict(attitude_reference=dict(generation=generation),
                        sensors=dict(attitude=dict(yaw_deg=i*90.,valid=True,age_ms=5))))),t+.02)
            r.close()
            self.assertEqual(r.snapshot["angular_samples"],0)
            self.assertGreater(r.snapshot["rejected"]["sensor_clock_gap"],0)

class SteeringLatchTests(unittest.TestCase):
    def test_straight_drive_does_not_assume_wheels_center_without_correction(self):
        m=MotionModel()
        m.advance(m.state,5,1.)
        turned=m.state["steering_latch"]
        self.assertGreater(turned,.5)
        m.advance(m.state,0,2.)
        m.advance(m.state,1,3.)
        self.assertEqual(m.state["steering_latch"],turned)
        m.advance(m.state,9,.1)
        self.assertGreater(m.state["steering_latch"],0.)
        self.assertLess(m.state["steering_latch"],turned)
        m.advance(m.state,9,.5)
        self.assertLess(m.state["steering_latch"],0.)
