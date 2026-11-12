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


if __name__=='__main__':unittest.main()
