"""Capture-clock metric odometry independent of visual processing latency."""
import copy
import threading
from multisensor_mapping import attitude_sample, sonar_observations


class MetricSensorWorker:
    def __init__(self, odometry, mount):
        self.odometry, self.mount = odometry, mount
        self.lock = threading.RLock()
        self.condition = threading.Condition()
        self.pending = None
        self.stopping = False
        self.thread = None
        self.last = None
        self.result = dict(odometry.status)

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name='metric-sensor-pose', daemon=True)
            self.thread.start()

    def offer(self, frame):
        # No estimator, disk write or visual-pipeline lock on camera delivery.
        with self.condition:
            self.pending = frame
            self.condition.notify()

    def observe(self, frame):
        with self.lock:
            packet = frame.get('sensor_packet') or {}
            key = (packet.get('boot_id'), packet.get('capture_monotonic_us'))
            if not key[0] or type(key[1]) is not int:
                return copy.deepcopy(self.result)
            if self.last and key[0] == self.last[0] and key[1] <= self.last[1]:
                return copy.deepcopy(self.result)
            sample = attitude_sample(frame)
            observations = sonar_observations(frame)
            if sample is None or self.mount is None:
                return copy.deepcopy(self.result)
            self.last = key
            calibration = ((packet.get('ahrs') or {}).get('ahrs') or {}).get('calibration') or {}
            self.result = self.odometry.update(observations,
                self.mount @ sample['R'] @ self.mount.T, ('ahrs', sample['epoch']), sample['time_s'],
                stationary_reference_reset=bool(calibration.get('gyro_bias_valid') and
                                                calibration.get('status') == 'succeeded'))
            return copy.deepcopy(self.result)

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.pending is not None or self.stopping)
                if self.stopping:
                    return
                frame, self.pending = self.pending, None
            try:
                self.observe(frame)
            except Exception as error:
                with self.lock:
                    self.result = {'valid':False, 'reason':'metric_sensor_error',
                                   'error':type(error).__name__}

    def pose_for(self, frame):
        with self.lock:
            current = self.observe(frame)
            observations = sonar_observations(frame)
            if not observations:
                return {'valid':False, 'reason':'no_capture_ranges'}
            time_s = sum(o['time_s'] for o in observations)/len(observations)
            pose = self.odometry.timeline.at(time_s, observations[0]['boot_id'])
            if pose is None:
                return {'valid':False, 'reason':'awaiting_metric_pose_at_capture',
                        'local_frame_id':self.odometry.local_frame_id}
            return {'valid':True, 'T_local_metric_camera':pose.tolist(), 'time_s':time_s,
                    'local_frame_id':self.odometry.local_frame_id, 'units':'m',
                    'coordinate_frame':'local_metric', 'source':'capture_clock_sensor_worker',
                    'qualification':'estimated_planar_sonar_odometry',
                    'velocity_m_s':current.get('velocity_m_s') if current.get('time_s')==time_s else None}

    def close(self):
        with self.condition:
            self.stopping = True
            self.condition.notify_all()
        if self.thread is not None:
            self.thread.join()
