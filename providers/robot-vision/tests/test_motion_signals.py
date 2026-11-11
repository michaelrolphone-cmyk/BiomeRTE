import copy
import math
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from motion_signals import build_response_model, summarize_trial, summarize_window


def baseline(**kw):
    return dict(boot_id='boot', frame='imu', accel_m_s2=[0, 0, 9.81],
                gyro_rad_s=[.02, -.01, .03], valid=True,
                age_upper_bound_s=.1, stationary_verified=True, **kw)


def sample(n, t, *, gyro=None, accel=None, **kw):
    return dict(boot_id='boot', frame='imu', sample_id=n, time_s=t,
                gyro_rad_s=gyro or [.02, -.01, .03], accel_m_s2=accel or [0, 0, 9.81],
                valid=True, age_upper_bound_s=.1, **kw)


def envelope(n, ms, *, boot='boot', age_ms=1):
    return {'boot_id':boot,'uptime_ms':ms+500,
            'cache_timing':{'elapsed_since_receive_s':.01,'request_duration_s':.01},
            'ahrs':{'imu':{'sample_count':n,'observed_uptime_ms':ms,'frame':'imu',
                           'accel_m_s2':[0,0,9.81],'gyro_rad_s':[0,0,1],
                           'valid':True,'age_ms':age_ms,'gyro_bias_applied':False},
                    'calibration':{'gyro_bias_rad_s':[0,0,0]},'attitude':{'generation':1}}}


def trial(envelopes):
    b=envelope(0,0); b['ahrs']['imu']['gyro_rad_s']=[0,0,0]
    return {'before':{'ahrs':b},'observations':[{'metadata':{'ahrs':e}} for e in envelopes],
            'command':{'direction':'forward','steer':'left','duration':.3},'events':[]}


class MotionSignalTests(unittest.TestCase):
    def test_bias_removed_from_known_rotation(self):
        rows=[sample(n,n*.1,gyro=[.02,-.01,.03+math.pi/2]) for n in range(11)]
        r=summarize_window(rows,baseline())
        self.assertAlmostEqual(r['window_yaw_delta_deg'],90)
        self.assertEqual(r['quality'],'observed_window')

    def test_projection_uses_measured_up_in_tilted_sensor(self):
        b=baseline(); b['accel_m_s2']=[0,-9.81,0]
        rows=[sample(n,n*.1,gyro=[.02,-1.01,.03],accel=[0,-9.81,0]) for n in range(11)]
        self.assertAlmostEqual(summarize_window(rows,b)['window_yaw_delta_deg'],math.degrees(1))

    def test_no_interpolation_across_missing_motion(self):
        rows=[sample(1,0,gyro=[0,0,1]),sample(2,2,gyro=[0,0,1])]
        r=summarize_window(rows,baseline())
        self.assertEqual(r['observed_yaw_delta_deg'],0)
        self.assertIsNone(r['window_yaw_delta_deg'])
        self.assertEqual(r['unobserved_gaps'][0]['duration_s'],2)

    def test_duplicate_samples_do_not_inflate_integration(self):
        rows=[sample(n,n*.1) for n in range(5)]
        r=summarize_window([x for row in rows for x in [row,row]],baseline())
        self.assertEqual(r['unique_samples'],5)
        self.assertEqual(r['duplicate_samples'],5)
        self.assertAlmostEqual(r['integrated_duration_s'],.4)

    def test_boot_or_frame_changes_are_rejected(self):
        rows=[sample(1,0),sample(2,.1)]; rows[1]['boot_id']='new'
        rows.append(sample(3,.2)); rows[2]['frame']='other'
        r=summarize_window(rows,baseline())
        self.assertEqual(r['rejected_samples'],{'different_boot':1,'different_frame':1})
        self.assertIsNone(r['window_yaw_delta_deg'])

    def test_calibration_mode_changes_break_window(self):
        b=baseline(); b['gyro_reference']='raw'
        a=sample(1,0); a['gyro_reference']='raw'
        c=sample(2,.1); c['gyro_reference']='corrected'
        self.assertEqual(summarize_window([a,c],b)['rejected_samples'],{'gyro_calibration_changed':1})

    def test_out_of_order_samples_are_not_sorted_into_false_motion(self):
        r=summarize_window([sample(1,.2),sample(2,.1)],baseline())
        self.assertEqual(r['unique_samples'],1)
        self.assertEqual(r['rejected_samples'],{'nonmonotonic_sensor_time':1})

    def test_stale_and_unknown_age_are_rejected(self):
        a=sample(1,0); a['age_upper_bound_s']=.6
        b=sample(2,.1); b.pop('age_upper_bound_s')
        r=summarize_window([a,b],baseline())
        self.assertEqual(r['rejected_samples'],{'stale_or_unbounded_age':2})

    def test_nonfinite_and_boolean_samples_are_invalid(self):
        a=sample(1,0); a['gyro_rad_s'][0]=float('nan')
        b=sample(2,.1); b['time_s']=True
        r=summarize_window([a,b],baseline())
        self.assertEqual(r['rejected_samples'],{'malformed_values':2})

    def test_unverified_stationarity_prevents_heading_claim(self):
        b=baseline(); b['stationary_verified']=False
        r=summarize_window([sample(n,n*.1) for n in range(4)],b)
        self.assertIsNone(r['window_yaw_delta_deg'])
        self.assertIn('stationary_baseline_unverified',r['quality_reasons'])

    def test_orientation_change_alone_is_not_magnitude_jolt(self):
        rows=[sample(1,0),sample(2,.1,accel=[9.81,0,0])]
        self.assertEqual(summarize_window(rows,baseline())['jolt_candidates'],[])

    def test_transient_is_candidate_without_contact_or_clearance_claim(self):
        r=summarize_window([sample(1,0),sample(2,.1,accel=[0,0,13])],baseline())
        self.assertAlmostEqual(r['jolt_candidates'][0]['magnitude_departure_m_s2'],3.19)
        self.assertFalse(r['contact_confirmed'])
        self.assertFalse(r['clearance_verified'])

    def test_adapter_uses_sensor_clock_and_deduplicates_republished_sample(self):
        r=summarize_trial(trial([envelope(1,0),envelope(1,0),envelope(2,100),envelope(3,200),envelope(4,300)]))
        self.assertEqual(r['unique_samples'],4)
        self.assertAlmostEqual(r['integrated_duration_s'],.3)
        self.assertEqual(r['duplicate_samples'],1)
        self.assertFalse(r['timing_qualified'])
        self.assertIsNone(r['response_delay_s'])

    def test_adapter_unwraps_firmware_clock_without_crossing_gap(self):
        period=1<<32
        r=summarize_trial(trial([envelope(1,period-200),envelope(2,period-100),envelope(3,0),envelope(4,100)]),stationary_verified=True)
        self.assertAlmostEqual(r['integrated_duration_s'],.3)
        self.assertAlmostEqual(r['window_yaw_delta_deg'],math.degrees(.3))

    def test_adapter_rejects_replay_not_wrap(self):
        r=summarize_trial(trial([envelope(1,300),envelope(2,200),envelope(3,400)]))
        self.assertEqual(r['rejected_samples'],{'nonmonotonic_sensor_time':1})

    def test_adapter_includes_network_and_receipt_age(self):
        e=envelope(1,0,age_ms=490)
        r=summarize_trial(trial([e]))
        self.assertEqual(r['rejected_samples'],{'stale_or_unbounded_age':1})

    def test_bad_baselines_and_bounds_fail_before_feature_computation(self):
        for field,value in [('valid',False),('age_upper_bound_s',None),('accel_m_s2',[0,0,0])]:
            b=baseline(); b[field]=value
            with self.assertRaises(ValueError):summarize_window([],b)
        with self.assertRaises(ValueError):summarize_window([],baseline(),max_gap_s=float('nan'))

    def test_response_model_keeps_transport_delay_distinct_from_physical_lag(self):
        j=trial([envelope(n,n*100) for n in range(4)])
        j['events']=[{'type':'accepted','accepted_sequence':7,'received':10},
                     {'type':'ack','stage':'applied','intent_sequence':7,'received':10.3}]
        model=build_response_model([('case',j)]); c=model['groups'][0]['cases'][0]
        self.assertAlmostEqual(c['accepted_to_applied_ack_arrival_s'][0],.3)
        self.assertIsNone(c['physical_response_delay_s'])
        self.assertIsNone(model['predictive_steering_model'])
        self.assertFalse(c['commanded_input_is_observed_motion'])


if __name__=='__main__':unittest.main()
