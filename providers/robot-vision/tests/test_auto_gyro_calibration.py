import copy
import threading
import unittest
import numpy as np
from auto_gyro_calibration import AutoGyroCalibration


def frame(t,boot='boot',state='uncalibrated',bias=False,gyro=None):
    identity={'receiver_id':'camera','boot_id':boot}
    return {'sensor_packet':{**identity,'capture_monotonic_us':int(t*1e6),
        'ahrs':{**identity,'ahrs':{'imu':{'valid':True,'observed_uptime_ms':int(t*1000),
            'gyro_raw_rad_s':gyro or [0.,0.,0.],'accel_m_s2':[0.,0.,9.81]},
            'calibration':{'status':state,'gyro_bias_valid':bias}}}}}


class AutomaticCalibrationTests(unittest.TestCase):
    def test_camera_noise_does_not_force_manual_calibration(self):
        model=AutoGyroCalibration(lambda:None)
        rng=np.random.default_rng(17)
        try:
            first=rng.normal(80,6,(240,320,3))
            second=rng.normal(80,6,(240,320,3))
            model.observe(frame(1),first);model.observe(frame(1.1),second)
            self.assertIsNotNone(model.future)
        finally:model.close()

    def test_two_fresh_static_frames_request_calibration_without_blocking(self):
        called=threading.Event();release=threading.Event()
        def request():called.set();release.wait(1);return {'accepted':True,'status':'queued'}
        model=AutoGyroCalibration(request)
        image=np.ones((24,32,3),dtype=np.uint8)*80
        try:
            model.observe(frame(1),image)
            result=model.observe(frame(1.1),image)
            self.assertTrue(called.wait(1))
            self.assertFalse(result['calibration_blocking'])
            self.assertEqual(result['state'],'calibration_requested')
            release.set();model.future.result(timeout=1)
            self.assertEqual(model.observe(frame(3.2,state='succeeded',bias=True),image)['state'],'calibrated')
        finally:release.set();model.close()

    def test_replayed_capture_does_not_look_like_two_frames(self):
        calls=[];model=AutoGyroCalibration(lambda:calls.append(1))
        image=np.zeros((24,32,3),np.uint8)
        try:
            model.observe(frame(1),image);model.observe(frame(1),image)
            self.assertEqual(calls,[]);self.assertIsNone(model.future)
        finally:model.close()

    def test_changed_image_or_rotating_imu_does_not_trigger(self):
        model=AutoGyroCalibration(lambda:None)
        try:
            model.observe(frame(1),np.zeros((24,32,3)))
            model.observe(frame(1.1),np.ones((24,32,3))*40)
            self.assertIsNone(model.future)
            model.observe(frame(1.2,gyro=[0,0,.5]),np.ones((24,32,3))*40)
            self.assertIsNone(model.future)
        finally:model.close()

    def test_boot_reset_rearms_but_valid_bias_needs_no_manual_step(self):
        model=AutoGyroCalibration(lambda:None)
        image=np.zeros((24,32,3))
        try:
            self.assertEqual(model.observe(frame(1,state='succeeded',bias=True),image)['state'],'calibrated')
            model.observe(frame(.1,boot='new'),image)
            model.observe(frame(.2,boot='new'),image)
            self.assertIsNotNone(model.future)
        finally:model.close()

    def test_firmware_collection_and_failed_motion_retry_use_fresh_evidence(self):
        model=AutoGyroCalibration(lambda: {'accepted':True})
        image=np.zeros((24,32,3))
        try:
            model.observe(frame(1),image);model.observe(frame(1.1),image)
            model.future.result(timeout=1)
            self.assertEqual(model.observe(frame(2,state='collecting'),image)['state'],'calibrating')
            model.observe(frame(4.2,state='motion_detected'),image)
            self.assertFalse(model.pending)
            model.observe(frame(4.3,state='motion_detected'),image)
            self.assertTrue(model.pending)
        finally:model.close()


if __name__=='__main__':unittest.main()
