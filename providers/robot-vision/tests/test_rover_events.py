import math
import unittest
from rover_events import MotionEvents, gravity, rotate, G

Q=[2**-.5,-2**-.5,0.,0.]
def frame(t, acceleration=None, yaw_rate=0., history=True, boot='boot'):
    samples=[]
    for k in range(30):
        x=round(t-.58+.02*k,6)
        yaw=yaw_rate*max(0,x-11.)
        z=math.radians(yaw)/2
        # world yaw times vertical-mounted IMU attitude
        q=[math.cos(z)*Q[0],math.cos(z)*Q[1],math.sin(z)*Q[1],math.sin(z)*Q[0]]
        up=gravity(q)
        world=acceleration(x) if acceleration else [0,0,0]
        body=rotate([q[0],-q[1],-q[2],-q[3]],world)
        samples.append(dict(monotonic_us=round(x*1e6),valid=True,attitude_valid=True,generation=1,
            accel_m_s2=[a+G*b for a,b in zip(body,up)],gyro_rad_s=[math.radians(yaw_rate)*v if x>=11 else 0 for v in up],
            quaternion_wxyz=q,yaw_deg=yaw))
    last=samples[-1]
    state=dict(worker_ready=True,paused=False,sample_rate_hz=50,
        attitude=dict(valid=True,generation=1,quaternion_wxyz=last['quaternion_wxyz'],yaw_deg=last['yaw_deg']),
        imu=dict(valid=True,age_ms=0,accel_m_s2=last['accel_m_s2'],gyro_rad_s=last['gyro_rad_s']))
    if history:state['imu_samples']=samples
    return dict(frame_id=boot+':'+str(t),capture_host_monotonic_interval_s=[t+990-.005,t+990+.005],
        sensor_packet=dict(boot_id=boot,capture_monotonic_us=round(t*1e6),ahrs=dict(ahrs=state)))

class Events(unittest.TestCase):
    def ready(self):
        d=MotionEvents()
        for t in [10,10.2,10.4,10.6,10.8]:d.observe(frame(t))
        self.applied(d,0,0,1000.8)
        return d
    def applied(self,d,mask,seq=1,t=1001):
        d.control(dict(stage='applied',commanded_mask=mask,sequence=seq,authority='a',epoch=1,generation=1,host_issue_age_s=.01),t)
    def test_stationary_vertical_mount(self):
        d=self.ready()
        for i in range(30):d.observe(frame(11+i*.2))
        self.assertFalse(d.snapshot()['events'])
    def test_no_response(self):
        d=self.ready();self.applied(d,1)
        for t in [11,11.2,11.4,11.6,11.8]:d.observe(frame(t))
        events=d.snapshot()['events']
        self.assertEqual([e['kind'] for e in events],['stuck'])
        self.assertEqual(d.snapshot()['state']['drive_response'],'no_response_observed')
    def test_requested_drive_is_not_applied(self):
        d=self.ready();d.control(dict(stage='requested',commanded_mask=1,sequence=1),1001)
        for t in [11,11.2,11.4,11.6,11.8]:d.observe(frame(t))
        self.assertFalse(d.snapshot()['events'])
    def test_launch_coast_brake(self):
        d=self.ready();self.applied(d,1)
        def a(t):return [0,-4 if 11<=t<11.1 else 3 if 11.4<t<11.5 else 0,0]
        for t in [11,11.2,11.4]:d.observe(frame(t,a))
        self.applied(d,0,2,1001.4)
        for t in [11.6,11.8,12]:d.observe(frame(t,a))
        self.assertFalse(d.snapshot()['events'])
    def test_sparse_cannot_prove_stuck(self):
        d=self.ready();self.applied(d,2)
        for t in [11,11.2,11.4,11.6,11.8]:d.observe(frame(t,history=False))
        self.assertFalse(d.snapshot()['events'])
        self.assertEqual(d.snapshot()['state']['drive_response'],'insufficient_time_aligned_imu_coverage')
    def test_both_turn_directions(self):
        for rate,direction in [(30,'left'),(-30,'right')]:
            d=self.ready()
            for i in range(12):d.observe(frame(11+i*.2,yaw_rate=rate))
            starts=[e for e in d.snapshot()['events'] if e['kind']=='turning' and e['phase']=='started']
            self.assertEqual([e['direction'] for e in starts],[direction])
    def test_pickup_accel_decel(self):
        d=self.ready()
        def a(t):return [0,0,3 if 11<=t<11.25 else -3 if 11.25<=t<11.5 else 0]
        for i in range(8):d.observe(frame(11+i*.2,a))
        self.assertEqual([e['kind'] for e in d.snapshot()['events']],['pickup'])
    def test_collision_opposes_drive(self):
        d=self.ready();self.applied(d,1)
        def a(t):return [0,5 if 11.3<=t<11.34 else -1 if 11<=t<11.1 else 0,0]
        for i in range(6):d.observe(frame(11+i*.2,a))
        self.assertIn('collision',[e['kind'] for e in d.snapshot()['events']])
    def test_duplicate_and_out_of_order(self):
        d=self.ready();d.observe(frame(11.4,yaw_rate=30));before=d.snapshot()
        d.observe(frame(11.4,yaw_rate=30));d.observe(frame(11,yaw_rate=-30))
        after=d.snapshot()
        before['state'].pop('telemetry_age_s',None);after['state'].pop('telemetry_age_s',None)
        self.assertEqual(after,before)
    def test_unobserved_initial_drive_is_not_a_new_pulse(self):
        d=MotionEvents()
        for t in [10,10.2,10.4,10.6,10.8]:d.observe(frame(t))
        self.applied(d,1)
        for t in [11,11.2,11.4,11.6,11.8]:d.observe(frame(t))
        self.assertFalse(d.snapshot()['events'])
    def test_invalid_and_old_generation_history(self):
        d=self.ready();f=frame(11)
        rows=f['sensor_packet']['ahrs']['ahrs']['imu_samples']
        for row in rows:row['generation']=2
        rows.append(None)
        self.assertFalse(d.observe(f)['state']['available'])
    def test_frame_events_reach_independent_metadata(self):
        from types import SimpleNamespace
        from vision_service import VisionService
        d=self.ready()
        service=object.__new__(VisionService);service.motion_events=d
        service.drive_response=SimpleNamespace(capture=lambda value:None)
        result=service._stream_motion_events(frame(11.4,yaw_rate=30))
        self.assertEqual(result['events'][0]['direction'],'left')

    def test_round_trip_jitter_does_not_reject_new_capture(self):
        d=self.ready()
        f=frame(11);f['capture_host_monotonic_interval_s']=[1000.95,1001.25]
        d.observe(f)
        g=frame(11.02);g['capture_host_monotonic_interval_s']=[1000.99,1001.05]
        self.assertEqual(d.observe(g)['state']['frame_id'],g['frame_id'])

    def test_reboot_clock_reset_does_not_silence_new_turn_events(self):
        d=self.ready()
        d.observe(frame(11.2,yaw_rate=30))
        for t in [11,11.2]:
            f=frame(t,yaw_rate=30,boot='reboot')
            f['capture_host_monotonic_interval_s']=[v+1 for v in f['capture_host_monotonic_interval_s']]
            f['sensor_packet']['capture_monotonic_us']-=10000000
            for row in f['sensor_packet']['ahrs']['ahrs']['imu_samples']:row['monotonic_us']-=10000000
            d.observe(f)
        starts=[e for e in d.snapshot()['events'] if e['kind']=='turning' and e['phase']=='started']
        self.assertEqual(len(starts),2)
        self.assertEqual(starts[-1]['boot_id'],'reboot')

    def test_history_and_events_bounded(self):
        d=self.ready()
        for i in range(100):d.observe(frame(11+i*.2,yaw_rate=30 if i%2 else -30))
        self.assertLessEqual(len(d.samples),300);self.assertLessEqual(len(d.snapshot()['events']),64)

if __name__=='__main__':unittest.main()
