"""Automatic stationary gyro calibration, independent of SLAM and live delivery."""
from concurrent.futures import ThreadPoolExecutor
import threading
import numpy as np


class AutoGyroCalibration:
    def __init__(self, request, *, enabled=True):
        self.request, self.enabled = request, enabled
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='auto-gyro-calibration')
        self.future = None
        self.epoch = None
        self.previous = None
        self.last_capture = None
        self.request_capture = None
        self.pending = False
        self.state = 'waiting_for_fresh_stationary_frames' if enabled else 'disabled'
        self.error = None

    def status(self):
        with self.lock:
            return {'enabled':self.enabled,'state':self.state,'error':self.error,
                    'calibration_blocking':False,'trigger':'two_distinct_stationary_camera_captures_and_quiet_imu'}

    def observe(self, frame, image):
        with self.lock:
            if not self.enabled:
                return self.status()
            packet=frame.get('sensor_packet') or {}
            raw=packet.get('ahrs') or {}
            imu=(raw.get('ahrs') or {}).get('imu') or {}
            calibration=(raw.get('ahrs') or {}).get('calibration') or {}
            capture=packet.get('capture_monotonic_us')
            epoch=(packet.get('receiver_id'),packet.get('boot_id'))
            if (not all(epoch) or type(capture) is not int
                    or any(raw.get(k)!=packet.get(k) for k in ('receiver_id','boot_id'))):
                self.previous=None
                return self.status()
            if epoch!=self.epoch:
                self.epoch=epoch;self.previous=None;self.last_capture=None
                self.request_capture=None;self.pending=False;self.error=None
            if self.last_capture is not None and capture<=self.last_capture:
                # SLAM and raw-video callbacks may observe the same capture.
                return self.status()
            self.last_capture=capture
            if self.future is not None and self.future.done():
                try:
                    self.future.result()
                    self.state='awaiting_firmware_calibration'
                except Exception as error:
                    self.error=type(error).__name__
                    self.state='reconciling_calibration_telemetry'
                self.future=None
            if calibration.get('gyro_bias_valid') is True:
                self.pending=False;self.previous=None;self.state='calibrated';self.error=None
                return self.status()
            firmware_state=calibration.get('status')
            if firmware_state in ('queued','collecting') or self.future is not None:
                self.pending=True;self.previous=None;self.state='calibrating'
                return self.status()
            if self.pending:
                # Reconcile a lost reply using later device telemetry, not a
                # repeated request on the same capture. Firmware measures 2s.
                if self.request_capture is None or capture-self.request_capture<3000000:
                    return self.status()
                if firmware_state not in ('uncalibrated','motion_detected','sampling_gap','interrupted','unavailable'):
                    return self.status()
                self.pending=False;self.previous=None
            observed=imu.get('observed_uptime_ms')
            if (not imu.get('valid') or type(observed) is not int
                    or (((capture//1000)-observed)&0xffffffff)>150
                    or frame.get('capture_age_at_decode_upper_s',0)>1.):
                self.previous=None;self.state='waiting_for_fresh_imu'
                return self.status()
            try:
                gyro=np.asarray(imu.get('gyro_raw_rad_s',imu.get('gyro_rad_s')),float)
                accel=np.asarray(imu.get('accel_m_s2'),float)
                quiet=(gyro.shape==(3,) and accel.shape==(3,) and np.isfinite(gyro).all()
                       and np.isfinite(accel).all() and np.linalg.norm(gyro)<.1
                       and abs(np.linalg.norm(accel)-9.81)<.6)
                pixels=np.asarray(image)
                # Area averages suppress ordinary camera/JPEG noise. Sampling
                # individual pixels made an unchanged dim scene look moving.
                rows,columns=min(24,pixels.shape[0]),min(32,pixels.shape[1])
                by,bx=pixels.shape[0]//rows,pixels.shape[1]//columns
                gray=pixels.mean(axis=2) if pixels.ndim==3 else pixels.astype(float)
                small=gray[:rows*by,:columns*bx].reshape(rows,by,columns,bx).mean(axis=(1,3))
            except (TypeError,ValueError,IndexError):
                self.previous=None;self.state='waiting_for_valid_frame_and_imu'
                return self.status()
            old=self.previous
            self.previous={'capture':capture,'image':small.copy(),'gyro':gyro,'accel':accel} if quiet else None
            if not quiet or old is None:
                self.state='waiting_for_stationary_frames'
                return self.status()
            if not 0<capture-old['capture']<=1000000 or small.shape!=old['image'].shape:
                return self.status()
            difference=np.abs(small-old['image'])
            stationary=(float(difference.mean())<=1.5 and float((difference>8).mean())<=.01
                        and np.linalg.norm(gyro-old['gyro'])<.02
                        and np.linalg.norm(accel-old['accel'])<.15)
            if stationary:
                self.future=self.executor.submit(self.request)
                self.request_capture=capture;self.pending=True;self.previous=None
                self.state='calibration_requested';self.error=None
            else:
                self.state='waiting_for_stationary_frames'
            return self.status()

    def close(self):
        self.executor.shutdown(wait=False,cancel_futures=True)
