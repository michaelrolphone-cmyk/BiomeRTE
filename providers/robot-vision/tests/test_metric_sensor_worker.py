import tempfile
import threading
import time
import unittest
from pathlib import Path
import numpy as np
from metric_sensor_worker import MetricSensorWorker
from multisensor_mapping import SonarMetricOdometry, estimated_rover_mounts
from test_multisensor_mapping import frame
import test_multisensor_mapping as mapping_tests


def room_frame(t):
    f = frame(t)
    rows = mapping_tests.MetricMappingTests.room_scan(t-.05, (t-.95)*.1)
    f['sensor_packet']['ultrasonic']['ultrasonic']['sensors'] = [
        {'id':o['sensor_id'], 'valid':True, 'status':'ok', 'sequence':round(t*1000),
         'observed_uptime_ms':round((t-.05)*1000), 'distance_m':float(o['distance_m'])} for o in rows]
    return f


class MetricWorkerTests(unittest.TestCase):
    def test_slow_visual_consumer_does_not_fragment_sensor_trajectory(self):
        with tempfile.TemporaryDirectory() as d:
            model = SonarMetricOdometry(Path(d)/'state', estimated_rover_mounts())
            worker = MetricSensorWorker(model, np.eye(3))
            identities = set()
            for i in range(40):
                t = 1+i*.2
                worker.observe(room_frame(t))
                if model.previous is not None:
                    identities.add(model.local_frame_id)
                # Visual processing catches up only every two seconds.
                if i in (10,20,30):
                    result = worker.pose_for(room_frame(t-.8))
                    self.assertTrue(result['valid'])
                    self.assertAlmostEqual(result['time_s'],t-.85,places=5)
                    self.assertAlmostEqual(result['T_local_metric_camera'][2][3],(t-1.2)*.1-.08,places=5)
            self.assertEqual(len(identities),1)

    def test_camera_offer_never_waits_for_estimator_disk_or_visual_lock(self):
        with tempfile.TemporaryDirectory() as d:
            worker = MetricSensorWorker(SonarMetricOdometry(Path(d)/'state',estimated_rover_mounts()),np.eye(3))
            done = threading.Event()
            with worker.lock:
                caller = threading.Thread(target=lambda:(worker.offer(room_frame(1)),done.set()))
                caller.start()
                self.assertTrue(done.wait(1))
            caller.join()
            worker.start()
            worker.close()

    def test_older_visual_frame_does_not_rewind_sensor_clock(self):
        with tempfile.TemporaryDirectory() as d:
            worker = MetricSensorWorker(SonarMetricOdometry(Path(d)/'state',estimated_rover_mounts()),np.eye(3))
            for i in range(15):worker.observe(room_frame(1+i*.2))
            last = worker.last
            worker.pose_for(room_frame(2))
            self.assertEqual(worker.last,last)

    def test_stationary_visual_imu_constraint_survives_changing_echoes(self):
        with tempfile.TemporaryDirectory() as d:
            worker = MetricSensorWorker(SonarMetricOdometry(Path(d)/'state',estimated_rover_mounts()),np.eye(3))
            image = np.tile(np.arange(320,dtype=float),(240,1))
            identity = None
            for i in range(30):
                f=room_frame(1+i*.2)
                raw=f['sensor_packet']['ahrs']['ahrs']
                raw['imu'].update(gyro_rad_s=[0,0,0],accel_m_s2=[0,-9.81,0])
                raw['calibration']={'gyro_bias_valid':True,'status':'succeeded'}
                # Multipath changes ranges without camera/IMU motion.
                for row in f['sensor_packet']['ultrasonic']['ultrasonic']['sensors']:
                    row['distance_m']=.4+(i%3)*.6
                result=worker.observe(f,image)
                if result.get('valid'):
                    identity=identity or result['local_frame_id']
                    self.assertEqual(result['local_frame_id'],identity)
                    np.testing.assert_allclose(np.asarray(result['T_local_metric_camera'])[:3,3],0,atol=1e-8)
            self.assertIsNotNone(identity)
            self.assertTrue(result['stationarity_observed'])

    def test_dark_or_changing_images_never_assert_stationarity(self):
        with tempfile.TemporaryDirectory() as d:
            worker=MetricSensorWorker(SonarMetricOdometry(Path(d)/'state',estimated_rover_mounts()),np.eye(3))
            for i in range(3):
                f=room_frame(1+i*.2);raw=f['sensor_packet']['ahrs']['ahrs']
                raw['imu'].update(gyro_rad_s=[0,0,0],accel_m_s2=[0,-9.81,0]);raw['calibration']={'gyro_bias_valid':True}
                self.assertFalse(worker._stationary(f,np.zeros((240,320))))
            image=np.tile(np.arange(320,dtype=float),(240,1))
            worker._stationary(room_frame(2),image)
            self.assertFalse(worker._stationary(room_frame(2.2),255-image))

    def test_mapping_consumes_capture_without_any_visual_processing(self):
        from multisensor_mapping import SonarObstacleMap
        from types import SimpleNamespace
        import importlib.util
        if importlib.util.find_spec('cv2') is None:
            self.skipTest('native vision-service dependencies required')
        from vision_service import VisionService
        with tempfile.TemporaryDirectory() as d:
            model=SonarMetricOdometry(Path(d)/'state',estimated_rover_mounts())
            ledger=SonarObstacleMap(Path(d)/'rays',estimated_rover_mounts())
            service=SimpleNamespace(map_id='map',revision=0,metric_odometry=model,obstacle_map=ledger)
            record=lambda f,o:VisionService._record_metric_sensor_frame(service,f,o)
            worker=MetricSensorWorker(model,np.eye(3),record)
            for i in range(12):worker.observe(room_frame(1+i*.2))
            status=ledger.summary('map')
            self.assertEqual(status['ray_count'],48)
            self.assertGreater(status['metric_observations'],30)
            self.assertFalse(worker.status()['mapping_write']['visual_processing_required'])
            ledger.close()

    def test_sonar_is_retained_when_capture_attitude_is_unavailable(self):
        recorded=[]
        with tempfile.TemporaryDirectory() as d:
            worker=MetricSensorWorker(SonarMetricOdometry(Path(d)/'state',estimated_rover_mounts()),np.eye(3),
                                     lambda f,o:recorded.extend(o))
            f=room_frame(1);f['sensor_packet']['ahrs']['ahrs']['attitude']['valid']=False
            result=worker.observe(f)
            self.assertFalse(result['valid'])
            self.assertEqual(len(recorded),4)

    def test_inconsistent_ranges_wait_for_relocalization_without_resetting_origin(self):
        with tempfile.TemporaryDirectory() as d:
            worker=MetricSensorWorker(SonarMetricOdometry(Path(d)/'state',estimated_rover_mounts()),np.eye(3))
            for i in range(5):worker.observe(room_frame(1+i*.2))
            identity=worker.odometry.local_frame_id
            for i in range(5,20):
                f=room_frame(1+i*.2)
                # Only parallel left/right beams: no observed forward position.
                sensors=f['sensor_packet']['ultrasonic']['ultrasonic']['sensors']
                for row in sensors:
                    if row['id'] in ('front','rear'):row['valid']=False
                worker.observe(f)
                self.assertEqual(worker.odometry.local_frame_id,identity)
            result=worker.observe(room_frame(5))
            self.assertTrue(result['valid'])
            self.assertEqual(result['local_frame_id'],identity)


if __name__=='__main__':unittest.main()
