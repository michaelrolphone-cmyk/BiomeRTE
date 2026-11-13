"""Bounded capture-clock motion evidence; no actuation and no acceleration odometry."""
import copy
import math
import threading
import time
from collections import deque

G = 9.80665

def finite(x):
    return type(x) in (int, float) and math.isfinite(x)

def vector(x, n=3):
    return list(map(float, x)) if isinstance(x, (list, tuple)) and len(x) == n and all(finite(v) for v in x) else None

def norm(v): return math.sqrt(sum(x*x for x in v))
def wrap(x): return (x+180.) % 360.-180.

def gravity(q):
    w,x,y,z = q
    return [2*(x*z-w*y), 2*(y*z+w*x), 1-2*(x*x+y*y)]

def rotate(q, v):
    w,x,y,z = q; a,b,c = v
    return [(1-2*(y*y+z*z))*a+2*(x*y-w*z)*b+2*(x*z+w*y)*c,
            2*(x*y+w*z)*a+(1-2*(x*x+z*z))*b+2*(y*z-w*x)*c,
            2*(x*z-w*y)*a+2*(y*z+w*x)*b+(1-2*(x*x+y*y))*c]

class MotionEvents:
    def __init__(self, config=None):
        self.config = dict(config or {})
        self.lock = threading.RLock()
        self.events = deque(maxlen=64)
        self.samples = deque(maxlen=300)
        self.drives = deque(maxlen=32)
        self.sequence = 0
        self.drive_response = 'awaiting_applied_drive'
        self.epoch = self.frame_key = self.control_epoch = self.control_sequence = None
        self.active = None
        self.control_connected = False
        self.control_known = False
        self.last_control = None
        self.last_observed = None
        self.last_capture_host = None
        self.previous = None
        self.turn = None
        self.turn_quiet = None
        self.bias = [0., 0., 0.]
        self.baseline = 0
        self.lift = None
        self.cooldown = {}
        self.state = {'available':False, 'reason':'awaiting_capture_imu'}

    def control(self, value, received=None):
        now = time.monotonic() if received is None else received
        with self.lock:
            if value.get('type') == 'gap' or (value.get('type') == 'state' and not value.get('ready')):
                self.control_connected = False
                self.control_known = False
                if self.active: self.active['uncertain'] = True
                self.active = None
                return
            self.control_connected = True
            if value.get('stage') != 'applied': return
            mask, seq = value.get('commanded_mask'), value.get('sequence')
            if type(mask) is not int or not 0 <= mask <= 63 or type(seq) is not int: return
            epoch = (value.get('authority'), value.get('epoch'), value.get('generation'))
            if epoch != self.control_epoch:
                if self.active: self.active['uncertain'] = True
                self.active = None
                self.control_epoch, self.control_sequence = epoch, None
                self.control_known = False
            if self.control_sequence is not None and seq <= self.control_sequence: return
            self.control_sequence, self.last_control = seq, now
            age = value.get('host_issue_age_s')
            uncertainty = float(age) if finite(age) and 0 <= age <= .25 else None
            direction = 'forward' if mask & 3 == 1 else 'reverse' if mask & 3 == 2 else None
            if self.active and self.active['direction'] != direction:
                self.active['end'] = now
                self.active['end_uncertainty_s'] = uncertainty
                self.active = None
            if direction and self.active is None:
                self.active = dict(id=str(epoch)+':'+str(seq), direction=direction,
                    start=now, end=None, uncertainty_s=uncertainty,
                    end_uncertainty_s=None, detected=False, uncertain=not self.control_known or uncertainty is None)
                self.drives.append(self.active)
            self.control_known = True

    def _emit(self, kind, phase, t, frame, evidence, **extra):
        key = kind + ':' + phase
        if t-self.cooldown.get(key, -1e20) < (.5 if kind=='turning' else 1.): return
        self.cooldown[key] = t
        self.sequence += 1
        self.events.append(dict(event_id='motion:'+str(self.epoch)+':'+str(self.sequence),
            sequence=self.sequence, type='robot.motion.'+kind, kind=kind, phase=phase, capture_time_s=t, timestamp=time.time(),
            frame_id=frame.get('frame_id'), boot_id=self.epoch[0],
            schema='biome.telemetry.event.v1', state='cleared' if phase=='ended' else 'active', severity='info' if kind=='turning' else 'warning',
            message={'turning':'Observed '+str(extra.get('direction'))+' turn','collision':'Suspected collision: opposing drive impact','stuck':'Applied drive without a measurable acceleration response','pickup':'Inferred pickup or loss of support'}[kind],
            assessment='sensor_inference', evidence=evidence, **extra))

    def _sensor_rows(self, frame):
        packet = frame.get('sensor_packet') or {}
        state = ((packet.get('ahrs') or {}).get('ahrs') or {})
        capture = packet.get('capture_monotonic_us')
        if type(capture) is not int or not packet.get('boot_id'): return [], state, packet
        rows = state.get('imu_samples')
        history = isinstance(rows, list)
        if not history:
            imu, att = state.get('imu') or {}, state.get('attitude') or {}
            age = imu.get('age_ms')
            rows = [dict(imu, monotonic_us=capture-int(age*1000) if finite(age) else None,
                quaternion_wxyz=att.get('quaternion_wxyz'), yaw_deg=att.get('yaw_deg'),
                generation=att.get('generation'), attitude_valid=att.get('valid'))]
        answer = []
        for r in rows[-128:]:
            if not isinstance(r,dict):continue
            us = r.get('monotonic_us'); a=vector(r.get('accel_m_s2')); gy=vector(r.get('gyro_rad_s')); q=vector(r.get('quaternion_wxyz'),4)
            if (r.get('valid') is not True or r.get('attitude_valid') is not True
                    or type(us) is not int or not 0 <= capture-us <= 1500000
                    or r.get('generation') != (state.get('attitude') or {}).get('generation')
                    or a is None or gy is None or q is None or abs(norm(q)-1) > .01): continue
            answer.append(dict(t=us/1e6, accel=a, gyro=gy, q=q,
                yaw=r.get('yaw_deg'), generation=r.get('generation'), history=history))
        return sorted(answer, key=lambda r:r['t']), state, packet

    def observe(self, frame):
        with self.lock:
            rows, raw, packet = self._sensor_rows(frame)
            interval=frame.get('capture_host_monotonic_interval_s')
            capture_host=sum(interval)/2 if vector(interval,2) and interval[1]>=interval[0] else None
            if self.epoch and packet.get('boot_id')!=self.epoch[0] and capture_host is not None and self.last_capture_host is not None and capture_host<self.last_capture_host:return self.snapshot()
            epoch = (packet.get('boot_id'), (raw.get('attitude') or {}).get('generation'))
            key = (epoch, packet.get('capture_monotonic_us'))
            if key == self.frame_key: return self.snapshot()
            if self.frame_key and epoch[0] == self.epoch[0] and finite(key[1]) and finite(self.frame_key[1]) and key[1] <= self.frame_key[1]: return self.snapshot()
            if self.epoch != epoch:
                self.epoch = epoch; self.previous = None; self.samples.clear()
                self.baseline = 0; self.bias = [0.,0.,0.]; self.lift = None
                self.turn = self.turn_quiet = None
                self.cooldown.clear()
                for d in self.drives: d['uncertain'] = True
            self.frame_key = key
            if not rows or raw.get('worker_ready') is not True or raw.get('paused') or packet['capture_monotonic_us']/1e6-rows[-1]['t']>.12:
                self.state = dict(available=False, reason='missing_or_invalid_capture_imu', turning=None)
                return self.snapshot()
            interval = frame.get('capture_host_monotonic_interval_s')
            host = sum(interval)/2 if vector(interval,2) and interval[1]>=interval[0] else None
            clock_error = (interval[1]-interval[0])/2 if host is not None else None
            self.last_observed=time.monotonic()
            if host is not None:self.last_capture_host=host
            capture = packet['capture_monotonic_us']/1e6
            for r in rows:
                t=r['t']; previous=self.previous
                if previous and t <= previous['t']: continue
                if previous and t-previous['t'] > .12:
                    self.turn=None; self.turn_quiet=None; self.lift=None
                up = gravity(r['q'])
                linear = [r['accel'][i]-G*up[i] for i in range(3)]
                world = rotate(r['q'], linear)
                if not self.active and norm(r['gyro']) < .04 and norm(world) < .6:
                    self.bias = [.98*b+.02*v for b,v in zip(self.bias,world)]
                    self.baseline += 1
                world = [v-b for v,b in zip(world,self.bias)]
                yaw_rate = math.degrees(sum(x*y for x,y in zip(r['gyro'],up)))
                r.update(world=world, rate=yaw_rate,
                    host=None if host is None else host-(capture-t), clock_error=clock_error)
                self.samples.append(r)
                dt = None if previous is None else t-previous['t']
                dy = wrap(r['yaw']-previous['yaw']) if previous and finite(r['yaw']) and finite(previous['yaw']) else None
                turn = ('left' if yaw_rate>0 else 'right') if dt and dt <= .12 and abs(yaw_rate)>=6 and dy is not None and dy*yaw_rate>0 and abs(dy)/dt>=4 else None
                if turn:
                    self.turn_quiet=None
                    if self.turn != turn:
                        if self.turn: self._emit('turning','ended',t,frame,{'yaw_rate_deg_s':yaw_rate},direction=self.turn)
                        self.turn=turn
                        self._emit('turning','started',t,frame,{'yaw_rate_deg_s':yaw_rate,'heading_delta_deg':dy,'heading_source':'relative_ahrs_yaw'},direction=turn)
                elif self.turn:
                    self.turn_quiet = self.turn_quiet or t
                    if t-self.turn_quiet >= .18:
                        self._emit('turning','ended',t,frame,{'yaw_rate_deg_s':yaw_rate},direction=self.turn)
                        self.turn=None
                horizontal=math.hypot(world[0],world[1]); vertical=world[2]
                jerk = norm([world[i]-previous['world'][i] for i in range(3)])/dt if dt and dt<=.08 else 0
                active = self.active if self.active and r['host'] is not None and r['host'] >= self.active['start'] else None
                forward_axis=rotate(r['q'], self.config.get('forward_axis_imu', [0.,0.,-1.]))
                longitudinal=sum(a*b for a,b in zip(world,forward_axis))
                opposing=-longitudinal if active and active['direction']=='forward' else longitudinal
                if active and not active['uncertain'] and r['host']-active['start']>.12 and opposing>=2.5 and horizontal >= self.config.get('impact_accel_m_s2',3.5) and jerk>=30 and horizontal>abs(vertical)*1.5:
                    self._emit('collision','suspected',t,frame,{'horizontal_accel_m_s2':horizontal,'jerk_m_s3':jerk,'opposing_accel_m_s2':opposing,'drive_id':active['id']},direction=active['direction'])
                if vertical>.9 and self.baseline>=10 and self.lift is None:
                    self.lift=dict(start=t,v=0.,z=0.,peak=vertical)
                if self.lift and dt and dt<=.08:
                    l=self.lift; l['v']+=vertical*dt; l['z']+=l['v']*dt; l['peak']=max(l['peak'],vertical)
                    if .06<t-l['start']<1.5 and l['z']>.025 and vertical<-.5:
                        self._emit('pickup','detected',t,frame,{'vertical_displacement_estimate_m':l['z'],'upward_accel_peak_m_s2':l['peak'],'deceleration_m_s2':vertical,'qualification':'short_window_inertial_lift_estimate_not_absolute_height'})
                        self.lift=None
                    elif t-l['start']>=1.5 or l['z']<-.03: self.lift=None
                if previous and norm(r['accel'])<3 and norm(previous['accel'])<3 and dt and dt<=.08:
                    self._emit('pickup','suspected',t,frame,{'reason':'loss_of_support_or_freefall','acceleration_norm_m_s2':norm(r['accel'])})
                self.previous=r
            self._drive_response(frame)
            self.state=dict(available=True, reason=None, turning=None if not self.turn else
                dict(direction=self.turn,yaw_rate_deg_s=self.previous['rate']),
                imu_history_available=any(r['history'] for r in rows),
                imu_sample_rate_hz=raw.get('sample_rate_hz'),
                drive_evidence_connected=self.control_connected, baseline_samples=self.baseline,
                drive_response=self.drive_response,
                frame_id=frame.get('frame_id'), capture_monotonic_us=packet.get('capture_monotonic_us'))
            return self.snapshot()

    def _drive_response(self, frame):
        if not self.samples: return
        now=self.samples[-1]['host']
        if now is None: return
        for d in self.drives:
            if d['detected'] or d['uncertain']: continue
            end=d['end']
            if end is not None and now<end+.25: continue
            if end is None and now<d['start']+.65: continue
            stop=min(end+.25 if end is not None else now,now)
            points=[r for r in self.samples if r['host'] is not None and d['start']-.1<=r['host']<=stop]
            covered=(len(points)>=8 and self.baseline>=10 and all(r['history'] for r in points)
                and (end is None or (d['end_uncertainty_s'] is not None and d['end_uncertainty_s']<=.1))
                and d['uncertainty_s'] is not None and d['uncertainty_s']<=.1
                and all(r['clock_error'] is not None and 0<=r['clock_error']<=.1 for r in points)
                and points[0]['host']<=d['start']+.04 and points[-1]['host']>=stop-.04
                and all(0<b['t']-a['t']<=.06 for a,b in zip(points,points[1:])))
            if not covered:
                self.drive_response='insufficient_time_aligned_imu_coverage'
                continue
            peak=max(norm(r['world']) for r in points)
            threshold=self.config.get('drive_response_accel_m_s2',.4)
            if peak<threshold and stop-d['start']>=.18:
                self._emit('stuck','suspected',self.samples[-1]['t'],frame,
                    {'reason':'drive_without_acceleration_or_deceleration_response','drive_id':d['id'],
                     'direction':d['direction'],'window_s':stop-d['start'],'sample_count':len(points),
                     'max_linear_accel_m_s2':peak,'threshold_m_s2':threshold,
                     'qualification':'does_not_prove_zero_velocity_or_distinguish_collision_from_other_stall'})
            self.drive_response='no_response_observed' if peak<threshold else 'acceleration_response_observed'
            d['detected']=True

    def snapshot(self):
        with self.lock:
            state=copy.deepcopy(self.state)
            age=None if self.last_observed is None else time.monotonic()-self.last_observed
            state['telemetry_age_s']=age
            if age is not None and age>1.:state.update(available=False,reason='telemetry_stale',turning=None)
            return dict(schema='biome.rover.events.v1', state=state,
                events=copy.deepcopy(list(self.events)), last_sequence=self.sequence,
                delivery='capture_frame_keyed_latest_telemetry', retention='bounded_64_events')
