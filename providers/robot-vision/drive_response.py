"""Drive response evidence from applied outputs and qualified high-rate IMU samples.

Detection reports suspected blockage, never a position or velocity measurement.
The monitor is passive: it subscribes to the shared control stream and reads the
existing AHRS cache. Camera-capture sensor association remains independent.
"""
import asyncio
from collections import deque
import copy
import json
import math
from pathlib import Path
import statistics
import struct
import threading
import time
import uuid


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def gravity_in_sensor(q):
    if not isinstance(q, (list, tuple)) or len(q) != 4 or not all(finite(v) for v in q):
        raise ValueError("invalid attitude")
    n = math.sqrt(sum(v*v for v in q))
    if abs(n-1.) > .002:
        raise ValueError("nonunit attitude")
    w,x,y,z = [v/n for v in q]
    return [2*(x*z-w*y)*9.80665, 2*(y*z+w*x)*9.80665,
            (1-2*(x*x+y*y))*9.80665]


class DriveResponseDetector:
    def __init__(self, config=None):
        cfg = config or {}
        self.baseline_s = float(cfg.get("baseline_s", .3))
        self.onset_s = float(cfg.get("onset_s", .3))
        self.settle_s = float(cfg.get("settle_s", .3))
        self.max_gap_s = float(cfg.get("max_sample_gap_s", .065))
        self.min_accel = float(cfg.get("minimum_acceleration_m_s2", .25))
        self.min_impulse = float(cfg.get("minimum_response_impulse_m_s", .012))
        self.max_clock_s = float(cfg.get("max_clock_uncertainty_s", .06))
        self.merge_gap_s = float(cfg.get("merge_gap_s", .08))
        if not all(finite(v) and v > 0 for v in (
                self.baseline_s,self.onset_s,self.settle_s,self.max_gap_s,
                self.min_accel,self.min_impulse,self.max_clock_s,self.merge_gap_s)):
            raise ValueError("invalid drive response thresholds")
        self.samples = deque(maxlen=2048)
        self.pending = deque(maxlen=16)
        self.recent = deque(maxlen=4)
        self.active = None
        self.current = None
        self.direction = None
        self.authority = None
        self.sequence = None
        self.boot = None
        self.last_sample_id = None
        self.last_assessment = {"state":"waiting_for_applied_drive"}
        self.accepted = self.rejected = 0

    def gap(self, reason):
        self.current = None
        self.pending.clear()
        self.direction = None
        self.sequence = None
        self.samples.clear()
        self.last_assessment = {"state":"unassessable", "reason":reason}

    def sample(self, at, acceleration, identity, boot, uncertainty=0.):
        if (not all(finite(v) for v in (at,acceleration,uncertainty))
                or uncertainty < 0 or uncertainty > self.max_clock_s):
            self.rejected += 1
            return
        if boot != self.boot:
            if self.boot is not None:
                self.gap("imu_boot_changed")
            self.boot = boot
            self.last_sample_id = None
        if identity == self.last_sample_id:
            return
        if self.samples and at <= self.samples[-1][0]:
            self.rejected += 1
            return
        value = (at, acceleration, uncertainty)
        self.samples.append(value)
        self.last_sample_id = identity
        self.accepted += 1
        if self.current:
            pulse = self.current
            if at <= pulse["start"] + self.onset_s:
                pulse["onset"].append(value)
            pulse["peak_abs"] = max(pulse["peak_abs"], abs(acceleration-pulse["baseline"]))
        for pulse in self.pending:
            if pulse["end"]-.05 <= at <= pulse["end"]+self.settle_s:
                pulse["stop"].append(value)

    def applied(self, mask, at, sequence, authority, uncertainty=0.):
        if (type(mask) is not int or mask < 0 or mask > 63 or mask & 3 == 3
                or type(sequence) is not int or not finite(at)
                or not finite(uncertainty) or not 0 <= uncertainty <= self.max_clock_s):
            self.gap("applied_output_timing_unqualified")
            return
        if authority != self.authority:
            self.gap("control_authority_changed")
            self.authority = authority
        if self.sequence is not None and sequence <= self.sequence:
            return
        self.sequence = sequence
        direction = 1 if mask & 1 else -1 if mask & 2 else 0
        if direction == self.direction:
            return  # Hold renewals are not new acceleration requests.
        previous = self.direction
        if self.current:
            self.current.update(end=at, stop=[s for s in self.samples if at-.05 <= s[0] <= at+self.settle_s])
            self.pending.append(self.current)
            self.current = None
        self.direction = direction
        if not direction:
            return
        if self.pending and self.pending[-1]["direction"] == direction and 0 <= at-self.pending[-1]["end"] <= self.merge_gap_s:
            self.current = self.pending.pop()
            self.current["end"] = None
            self.current["stop"] = []
            self.current["merged_pulses"] += 1
            return
        baseline = [s for s in self.samples if at-self.baseline_s <= s[0] <= at-.01]
        center = statistics.median([s[1] for s in baseline]) if baseline else 0.
        noise = statistics.median([abs(s[1]-center) for s in baseline]) if baseline else 0.
        self.current = dict(id=uuid.uuid4().hex,start=at,end=None,direction=direction,
            command_sequence=sequence,command_mask=mask,control_authority=list(authority),
            baseline=center,noise=noise,baseline_samples=baseline,
            onset=[s for s in self.samples if at <= s[0] <= at+self.onset_s],
            stop=[],peak_abs=0.,merged_pulses=1,
            initial_state_known=previous is not None,clock_uncertainty=uncertainty)

    def coverage(self, samples, lo, hi):
        rows = sorted(s for s in samples if lo-self.max_gap_s <= s[0] <= hi+self.max_gap_s)
        inside = [s for s in rows if lo <= s[0] <= hi]
        if len(inside) < 3 or inside[0][0]-lo > self.max_gap_s or hi-inside[-1][0] > self.max_gap_s:
            return False
        return all(b[0]-a[0] <= self.max_gap_s for a,b in zip(inside,inside[1:]))

    def response(self, samples, baseline, sign, threshold):
        rows = sorted(samples)
        impulse = 0.
        sustained = 0
        longest = 0
        peak = 0.
        for i,s in enumerate(rows):
            a = sign*(s[1]-baseline)
            peak = max(peak,a)
            sustained = sustained+1 if a >= threshold else 0
            longest = max(longest,sustained)
            if i:
                dt = s[0]-rows[i-1][0]
                if 0 < dt <= self.max_gap_s:
                    impulse += max(0.,a)*dt
        return longest >= 2 and impulse >= self.min_impulse, peak, impulse

    def advance(self, now, utc=None):
        while self.pending and now >= self.pending[0]["end"]+self.settle_s+self.max_clock_s:
            pulse = self.pending.popleft()
            lo,hi = pulse["start"],pulse["end"]
            qualified = (pulse["initial_state_known"]
                and self.coverage(pulse["baseline_samples"],lo-self.baseline_s,lo-.01)
                and self.coverage(pulse["onset"],lo,min(hi,lo+self.onset_s))
                and self.coverage(pulse["stop"],hi-.05,hi+self.settle_s))
            evidence = dict(direction="forward" if pulse["direction"] == 1 else "reverse",
                duration_ms=round((hi-lo)*1000),command_sequence=pulse["command_sequence"],
                applied_mask=pulse["command_mask"],merged_pulses=pulse["merged_pulses"],
                baseline_samples=len(pulse["baseline_samples"]),onset_samples=len(pulse["onset"]),
                stop_samples=len(pulse["stop"]),sample_gap_limit_ms=round(self.max_gap_s*1000),
                baseline_noise_m_s2=round(pulse["noise"],4),
                actuation_evidence="relay_command_applied_no_physical_contact_feedback",
                drive_interval_host_monotonic_s=[lo,hi],imu_boot_id=self.boot)
            if not qualified or hi-lo < self.max_gap_s*2 or pulse["noise"] > self.min_accel:
                self.last_assessment = dict(state="unassessable",reason="insufficient_timed_imu_coverage",
                                            pulse_id=pulse["id"],evidence=evidence)
                continue
            threshold = max(self.min_accel, 6*pulse["noise"])
            onset,peak_onset,impulse_onset = self.response(pulse["onset"],pulse["baseline"],pulse["direction"],threshold)
            brake,peak_brake,impulse_brake = self.response(pulse["stop"],pulse["baseline"],-pulse["direction"],threshold)
            evidence.update(threshold_m_s2=round(threshold,4),
                onset_peak_m_s2=round(peak_onset,4),braking_peak_m_s2=round(peak_brake,4),
                onset_impulse_m_s=round(impulse_onset,4),braking_impulse_m_s=round(impulse_brake,4))
            if onset or brake:
                self.last_assessment = dict(state="response_observed" if onset and brake else "partial_response",
                                            pulse_id=pulse["id"],evidence=evidence)
                if self.active:
                    event = dict(self.active,state="cleared",timestamp=utc or time.time(),
                                 clear_reason="acceleration_response_observed")
                    self.recent.append(event)
                    self.active = None
            elif pulse["peak_abs"] > threshold:
                self.last_assessment = dict(state="unassessable",reason="ambiguous_acceleration_or_vibration",
                                            pulse_id=pulse["id"],evidence=evidence)
            else:
                self.last_assessment = dict(state="no_response",pulse_id=pulse["id"],evidence=evidence)
                if self.active is None:
                    self.active = dict(schema="biome.telemetry.event.v1",event_id=uuid.uuid4().hex,
                        type="robot.motion.no_drive_response",severity="warning",state="active",
                        classification="suspected_stuck_or_collision",
                        message="Drive applied without a measurable acceleration/deceleration response",
                        timestamp=utc or time.time(),pulse_id=pulse["id"],evidence=evidence)
                    self.recent.append(copy.deepcopy(self.active))
                else:
                    self.active.update(timestamp=utc or time.time(),pulse_id=pulse["id"],evidence=evidence)

    def snapshot(self):
        return dict(schema="biome.drive_response.v1",events=list(self.recent),
            active_events=[] if self.active is None else [copy.deepcopy(self.active)],
            last_assessment=copy.deepcopy(self.last_assessment),
            sampling=dict(accepted=self.accepted,rejected=self.rejected,
                          target_rate_hz=50,max_sample_gap_ms=round(self.max_gap_s*1000)),
            qualification="suspected_blockage_not_proof_of_no_motion")


class DriveResponseMonitor:
    def __init__(self, source, config=None):
        self.config = dict(config or {})
        self.enabled = self.config.get("enabled",False) is True
        self.source = source
        self.detector = DriveResponseDetector(self.config)
        self.axis = self.config.get("forward_axis_in_imu",[0.,0.,-1.])
        if len(self.axis) != 3 or not all(finite(v) for v in self.axis) or abs(sum(v*v for v in self.axis)-1.) > .002:
            raise ValueError("drive response axis must be a unit vector")
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.threads = []
        self.control_connected = False
        self.error = None

    def start(self):
        if not self.enabled:
            return self
        self.source.poll_interval_s = min(self.source.poll_interval_s,.02)
        self.threads = [threading.Thread(target=self._sample_loop,name="drive-response-imu",daemon=True),
                        threading.Thread(target=lambda:asyncio.run(self._control_loop()),name="drive-response-control",daemon=True)]
        for thread in self.threads:
            thread.start()
        return self

    def _sample_loop(self):
        while not self.stop.is_set():
            try:
                snap = self.source.snapshot()
                if isinstance(snap,dict):
                    imu = snap["sensors"]["imu"]
                    attitude = snap["sensors"]["attitude"]
                    if (imu.get("valid") and attitude.get("valid")
                            and imu.get("age_upper_bound_ms",1e9) <= 100
                            and attitude.get("age_upper_bound_ms",1e9) <= 100):
                        accel = imu["data"]["accel_m_s2"]
                        gravity = gravity_in_sensor(attitude["data"]["quaternion_wxyz"])
                        linear = sum(self.axis[i]*(accel[i]-gravity[i]) for i in range(3))
                        with self.lock:
                            self.detector.sample(imu["observed_monotonic_s"],linear,
                                imu["sample_identity"],snap["boot_id"],imu["time_uncertainty_s"])
                with self.lock:
                    self.detector.advance(time.monotonic())
            except Exception as exc:
                self.error = "imu:"+type(exc).__name__
            self.stop.wait(.01)

    async def _control_loop(self):
        from aiohttp import ClientSession, WSMsgType
        while not self.stop.is_set():
            try:
                token = Path(self.config["control_token_file"]).read_text().strip()
                async with ClientSession(headers={"Authorization":"Bearer "+token}) as session:
                    async with session.ws_connect("http://127.0.0.1:8093/api/live/control",heartbeat=20,max_msg_size=0) as socket:
                        self.control_connected = True
                        async for message in socket:
                            if self.stop.is_set():
                                break
                            if message.type not in (WSMsgType.TEXT,WSMsgType.BINARY):
                                continue
                            raw = message.data
                            if isinstance(raw,bytes):
                                size = struct.unpack("!I",raw[:4])[0]
                                value = json.loads(raw[4:4+size])
                            else:
                                value = json.loads(raw)
                            received = time.monotonic()
                            if value.get("stage") == "applied":
                                issued = value.get("issued_utc_ms")
                                age = time.time()-issued/1000. if finite(issued) else None
                                with self.lock:
                                    if age is None or age < -.02 or age > 2*self.detector.max_clock_s:
                                        self.detector.gap("applied_output_clock_unqualified")
                                    else:
                                        self.detector.applied(value.get("commanded_mask"),
                                            received-max(0.,age)/2.,value.get("sequence"),
                                            (value.get("authority"),value.get("epoch"),value.get("generation")),
                                            max(0.,age)/2.)
                            elif value.get("type") == "state" and not value.get("ready"):
                                with self.lock:
                                    self.detector.gap("control_not_ready")
            except Exception as exc:
                self.error = "control:"+type(exc).__name__
            finally:
                self.control_connected = False
                with self.lock:
                    self.detector.gap("control_disconnected")
            await asyncio.sleep(.5)

    def snapshot(self):
        with self.lock:
            result = self.detector.snapshot()
        result["sampling"]["last_sample_age_ms"] = (None if not self.detector.samples else
            max(0.,time.monotonic()-self.detector.samples[-1][0])*1000.)
        result.update(enabled=self.enabled,control_connected=self.control_connected,error=self.error,
                      source="applied_control_and_gravity_compensated_imu")
        return result

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=2.)


class CaptureDriveResponseMonitor(DriveResponseMonitor):
    """Preserve pulse-curve assessment while replacing the HTTP polling loop."""
    def start(self):
        return self

    def control(self,value,received):
        if not self.enabled:return
        with self.lock:
            if value.get('type')=='gap' or (value.get('type')=='state' and not value.get('ready')):
                self.control_connected=False
                self.detector.gap('control_unavailable')
                return
            self.control_connected=True
            if value.get('stage')!='applied':return
            age=value.get('host_issue_age_s')
            if not finite(age) or not 0<=age<=2*self.detector.max_clock_s:
                self.detector.gap('applied_output_clock_unqualified')
                return
            self.detector.applied(value.get('commanded_mask'),received-age/2,
                value.get('sequence'),(value.get('authority'),value.get('epoch'),value.get('generation')),age/2)

    def capture(self,motion):
        if not self.enabled:return
        from rover_events import rotate
        with motion.lock:
            rows=list(motion.samples)
            epoch=motion.epoch
        with self.lock:
            for r in rows:
                if self.detector.boot==epoch and self.detector.samples and r['host'] is not None and r['host']<=self.detector.samples[-1][0]:continue
                if not r['history'] or r['host'] is None or r['clock_error'] is None:continue
                axis=rotate(r['q'],self.axis)
                acceleration=sum(a*b for a,b in zip(r['world'],axis))
                self.detector.sample(r['host'],acceleration,(epoch,r['t']),epoch,r['clock_error'])
            if rows and rows[-1]['host'] is not None:self.detector.advance(rows[-1]['host'])

    def close(self):
        return
