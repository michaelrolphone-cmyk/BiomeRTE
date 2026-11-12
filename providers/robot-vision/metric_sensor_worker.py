"""Capture-clock metric odometry independent of visual processing latency."""
import copy
import threading
import numpy as np
from multisensor_mapping import attitude_sample, sonar_observations


class MetricSensorWorker:
    def __init__(self, odometry, mount, record=None):
        self.odometry, self.mount = odometry, mount
        self.record = record
        self.lock = threading.RLock()
        self.condition = threading.Condition()
        self.pending = None
        self.stopping = False
        self.thread = None
        self.last = None
        self.previous_image = None
        self.result = dict(odometry.status)

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name='metric-sensor-pose', daemon=True)
            self.thread.start()

    def status(self):
        with self.lock:
            return dict(copy.deepcopy(self.result), capture_identity=self.last,
                        delivery='independent_capture_clock_sensor_worker')

    def offer(self, frame, image=None):
        # No estimator, disk write or visual-pipeline lock on camera delivery.
        with self.condition:
            self.pending = (frame, image)
            self.condition.notify()

    def _stationary(self, frame, image):
        if image is None:
            return False
        packet = frame.get('sensor_packet') or {}
        raw = ((packet.get('ahrs') or {}).get('ahrs') or {})
        imu = raw.get('imu') or {}
        try:
            pixels = np.asarray(image)
            gray = pixels.mean(axis=2) if pixels.ndim==3 else pixels.astype(float)
            h,w = gray.shape
            by,bx = h//24,w//32
            small = gray[:24*by,:32*bx].reshape(24,by,32,bx).mean(axis=(1,3))
            accel = np.asarray(imu.get('accel_m_s2'),float)
            quiet = (imu.get('valid') and (raw.get('calibration') or {}).get('gyro_bias_valid')
                     and np.linalg.norm(imu.get('gyro_rad_s'))<.02
                     and accel.shape==(3,) and abs(np.linalg.norm(accel)-9.81)<.6)
            key = (packet.get('boot_id'),packet['capture_monotonic_us'])
            old = self.previous_image
            self.previous_image = (key,small.copy())
            if not quiet or old is None or key[0]!=old[0][0] or not 0<key[1]-old[0][1]<=1000000:
                return False
            difference = np.abs(small-old[1])
            return bool(small.std()>5 and difference.mean()<=1.5 and (difference>8).mean()<=.01)
        except (TypeError,ValueError,KeyError,IndexError):
            self.previous_image = None
            return False

    def observe(self, frame, image=None):
        with self.lock:
            packet = frame.get('sensor_packet') or {}
            key = (packet.get('boot_id'), packet.get('capture_monotonic_us'))
            if not key[0] or type(key[1]) is not int:
                return copy.deepcopy(self.result)
            if self.last and key[0] == self.last[0] and key[1] <= self.last[1]:
                return copy.deepcopy(self.result)
            sample = attitude_sample(frame)
            observations = sonar_observations(frame)
            self.last = key
            stationary = self._stationary(frame,image)
            calibration = ((packet.get('ahrs') or {}).get('ahrs') or {}).get('calibration') or {}
            if sample is None or self.mount is None:
                self.result = {'valid':False,'reason':'awaiting_capture_attitude'}
            else:
                self.result = self.odometry.update(observations,
                    self.mount @ sample['R'] @ self.mount.T, ('ahrs', sample['epoch']), sample['time_s'],
                    stationary_reference_reset=bool(calibration.get('gyro_bias_valid') and
                                                    calibration.get('status') == 'succeeded'),
                    stationary_observed=stationary)
            if self.record is not None:
                self.result['mapping_write'] = self.record(frame,observations)
            return copy.deepcopy(self.result)

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.pending is not None or self.stopping)
                if self.stopping:
                    return
                pending, self.pending = self.pending, None
            try:
                self.observe(*pending)
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
