import importlib.util
import math
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from drive_response import DriveResponseDetector, DriveResponseMonitor, gravity_in_sensor


class DriveResponseTests(unittest.TestCase):
    def run_curve(self, direction=1, moving=False, gap=False, repeated=False,
                  start=1.,end=1.6,last=2.1, identity_reused=False, uncertainty=0.):
        d = DriveResponseDetector()
        authority = ("controller","control-epoch",4)
        d.applied(0,.4,0,authority)
        for i in range(25,int(last*50)+1):
            t = i/50.
            a = direction*.8 if moving and start <= t < start+.12 else (
                -direction*.8 if moving and end <= t < end+.12 else 0.)
            if not gap or not start-.02 <= t <= end+.16:
                d.sample(t,a,"same" if identity_reused else str(i),"boot",uncertainty)
            if abs(t-start)<.001:
                d.applied(1 if direction==1 else 2,t,1,authority)
            if repeated and start < t < end and i%5==0:
                d.applied(1 if direction==1 else 2,t,i,authority)
            if abs(t-end)<.001:
                d.applied(0,t,10000,authority)
            d.advance(t,utc=1234.)
        return d

    def test_applied_drive_without_curve_emits_event_with_evidence(self):
        d = self.run_curve()
        event = d.snapshot()["active_events"][0]
        self.assertEqual("robot.motion.no_drive_response",event["type"])
        self.assertEqual("suspected_stuck_or_collision",event["classification"])
        self.assertEqual(600,event["evidence"]["duration_ms"])
        self.assertGreater(event["evidence"]["onset_samples"],5)
        self.assertEqual([1.,1.6],event["evidence"]["drive_interval_host_monotonic_s"])

    def test_normal_onset_and_deceleration_do_not_emit_blockage(self):
        d = self.run_curve(moving=True)
        self.assertEqual([],d.snapshot()["events"])
        self.assertEqual("response_observed",d.last_assessment["state"])

    def test_reverse_uses_reversed_signed_response(self):
        d = self.run_curve(direction=-1,moving=True)
        self.assertEqual("response_observed",d.last_assessment["state"])
        self.assertFalse(d.active)
        blocked = self.run_curve(direction=-1)
        self.assertEqual("reverse",blocked.active["evidence"]["direction"])

    def test_near_zero_steady_segment_is_not_misclassified(self):
        d = self.run_curve(moving=True,end=8.,last=8.5)
        self.assertEqual("response_observed",d.last_assessment["state"])

    def test_missing_imu_is_unassessable_not_blockage(self):
        d = self.run_curve(gap=True)
        self.assertEqual("unassessable",d.last_assessment["state"])
        self.assertFalse(d.active)

    def test_reused_imu_sample_does_not_establish_coverage(self):
        d = self.run_curve(identity_reused=True)
        self.assertEqual("unassessable",d.last_assessment["state"])
        self.assertFalse(d.active)

    def test_bad_sample_clock_does_not_establish_coverage(self):
        d = self.run_curve(uncertainty=.2)
        self.assertGreater(d.rejected,0)
        self.assertFalse(d.active)

    def test_hold_renewals_do_not_create_new_pulses(self):
        d = self.run_curve(moving=True,repeated=True)
        self.assertEqual("response_observed",d.last_assessment["state"])
        self.assertEqual(1,d.last_assessment["evidence"]["merged_pulses"])

    def test_long_drive_preserves_onset_without_unbounded_raw_history(self):
        d = self.run_curve(moving=True,end=60.,last=60.5)
        self.assertEqual("response_observed",d.last_assessment["state"])
        self.assertLessEqual(len(d.samples),2048)

    def test_too_short_pulse_is_not_declared_stuck(self):
        d = self.run_curve(end=1.1,last=1.6)
        self.assertEqual("unassessable",d.last_assessment["state"])
        self.assertFalse(d.active)

    def test_authority_gap_discards_incomplete_pulse(self):
        d = DriveResponseDetector()
        d.applied(0,0.,0,("a","e",1))
        d.applied(1,.5,1,("a","e",1))
        d.gap("control_disconnected")
        d.advance(10.)
        self.assertFalse(d.active)
        self.assertEqual("control_disconnected",d.last_assessment["reason"])

    def test_boot_change_does_not_join_different_imu_epochs(self):
        d = self.run_curve()
        d.sample(3.,0.,"new","new-boot")
        self.assertEqual("imu_boot_changed",d.last_assessment["reason"])

    def test_repeated_blocked_pulses_update_one_active_event(self):
        d = self.run_curve()
        event_id = d.active["event_id"]
        authority = ("controller","control-epoch",4)
        for i in range(110,201):
            t=i/50.;d.sample(t,0.,str(i),"boot")
            if i==130:d.applied(1,t,10001,authority)
            if i==160:d.applied(0,t,10002,authority)
            d.advance(t)
        self.assertEqual(event_id,d.active["event_id"])
        self.assertEqual(1,len(d.recent))

    def test_observed_response_clears_previous_event(self):
        d = self.run_curve()
        authority = ("controller","control-epoch",4)
        for i in range(110,201):
            t=i/50.
            a=.8 if 2.6 <= t < 2.72 else -.8 if 3.2 <= t < 3.32 else 0.
            d.sample(t,a,str(i),"boot")
            if i==130:d.applied(1,t,10001,authority)
            if i==160:d.applied(0,t,10002,authority)
            d.advance(t)
        self.assertIsNone(d.active)
        self.assertEqual("cleared",d.recent[-1]["state"])

    def test_gravity_compensation_supports_vertical_imu_mount(self):
        g=gravity_in_sensor([math.sqrt(.5),-math.sqrt(.5),0.,0.])
        self.assertAlmostEqual(0.,g[0])
        self.assertAlmostEqual(-9.80665,g[1])
        self.assertAlmostEqual(0.,g[2])

    def test_disabled_monitor_does_not_change_source_cadence(self):
        class Source:
            poll_interval_s=.1
        source=Source()
        m=DriveResponseMonitor(source)
        m.start()
        self.assertEqual(.1,source.poll_interval_s)
        self.assertFalse(m.snapshot()["enabled"])


    def test_brief_neutral_renewal_gap_merges_one_motion_window(self):
        d = DriveResponseDetector()
        authority=("controller","epoch",1)
        d.applied(0,.4,0,authority)
        for i in range(25,106):
            t=i/50.
            a=.8 if 1. <= t < 1.12 else -.8 if 1.6 <= t < 1.72 else 0.
            d.sample(t,a,str(i),"boot")
            for at,mask,seq in [(1.,1,1),(1.2,0,2),(1.24,1,3),(1.6,0,4)]:
                if abs(t-at)<.001:d.applied(mask,t,seq,authority)
            d.advance(t)
        self.assertEqual("response_observed",d.last_assessment["state"])
        self.assertEqual(2,d.last_assessment["evidence"]["merged_pulses"])

    def test_one_isolated_spike_is_ambiguous_not_proof_of_motion_or_blockage(self):
        d = DriveResponseDetector()
        authority=("a","e",1)
        d.applied(0,.4,0,authority)
        for i in range(25,106):
            t=i/50.;d.sample(t,1. if i==55 else 0.,str(i),"boot")
            if i==50:d.applied(1,t,1,authority)
            if i==80:d.applied(0,t,2,authority)
            d.advance(t)
        self.assertEqual("ambiguous_acceleration_or_vibration",d.last_assessment["reason"])
        self.assertFalse(d.active)

    def test_radio_budget_trimming_preserves_event(self):
        from stream_telemetry import bounded_metadata
        d=self.run_curve()
        state=d.snapshot()
        event=state["events"][0]
        metadata={"ahrs":{},"events":[event],
                  "radio":{"telemetry":{"telemetry":{"wifi":[{"name":"x"*300,"rssi_dbm":-60} for _ in range(50)]}}}}
        fitted=bounded_metadata(metadata,maximum=2500)
        self.assertEqual(event,fitted["events"][0])
        self.assertGreater(fitted["radio"]["telemetry"]["stream_scan_rows_omitted"],0)

    def test_live_camera_metadata_carries_event_and_capture_identity(self):
        from types import SimpleNamespace
        from vision_service import VisionService
        service=VisionService.__new__(VisionService)
        service.map_id="map";service.revision=3;service.session_id="session"
        service.calibration={"calibration_id":"cal"}
        detector=self.run_curve()
        service.drive_response=SimpleNamespace(snapshot=detector.snapshot)
        service._stream_radio=lambda frame: {}
        service._stream_ahrs=lambda frame: {}
        service._stream_ultrasonic=lambda: {}
        captured=SimpleNamespace(metadata={"frame_id":"boot:123","receive_decode_time":"now"},
                                 decoded_monotonic=10.)
        metadata=service._raw_live_metadata(captured,{})
        self.assertEqual("boot:123",metadata["frame_id"])
        self.assertEqual("robot.motion.no_drive_response",metadata["events"][0]["type"])
        self.assertEqual("no_response",metadata["drive_response"]["last_assessment"]["state"])


if __name__ == "__main__":
    unittest.main()
