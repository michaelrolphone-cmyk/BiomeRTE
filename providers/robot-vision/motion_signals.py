"""Passive AHRS event-window features. No navigation or actuator authority.

Sensor clocks, boots, frames, freshness and baseline provenance are explicit.
Partial integrations never become a whole-window heading or contact claim.
"""
import argparse
import json
import math
from collections import Counter
from pathlib import Path

UINT32_MS = 1 << 32


def _finite(x):
    return type(x) in (int, float) and math.isfinite(x)


def _vec(v):
    return isinstance(v, (list, tuple)) and len(v) == 3 and all(map(_finite, v))


def _norm(v):
    return math.sqrt(sum(x*x for x in v))


def summarize_window(samples, baseline, *, max_gap_s=.5,
                     max_sample_age_s=.5, jolt_threshold_m_s2=2.,
                     turn_threshold_deg=3.):
    """Integrate observed intervals about stationary measured up.

    Inputs use seconds, m/s² and rad/s in one named sensor frame. Baseline
    requires boot_id/frame, accel_m_s2/gyro_rad_s, valid, age_upper_bound_s,
    and stationary_verified. Sample identities are (boot_id, sample_id).
    Unconfirmed stationary baselines may yield provisional features, never
    qualified headings. Samples must arrive in monotonic sensor-time order.
    """
    bounds=(max_gap_s,max_sample_age_s,jolt_threshold_m_s2,turn_threshold_deg)
    if any(not _finite(x) or x <= 0 for x in bounds):
        raise ValueError('Signal bounds must be finite and positive')
    if (not isinstance(baseline,dict) or not baseline.get('boot_id')
            or not baseline.get('frame') or baseline.get('valid') is not True
            or not _vec(baseline.get('accel_m_s2'))
            or not _vec(baseline.get('gyro_rad_s'))
            or not _finite(baseline.get('age_upper_bound_s'))
            or not 0 <= baseline['age_upper_bound_s'] <= max_sample_age_s):
        raise ValueError('A valid fresh identified baseline is required')
    gravity=_norm(baseline['accel_m_s2'])
    if not 8 <= gravity <= 12:
        raise ValueError('Stationary gravity baseline is implausible')
    up=[x/gravity for x in baseline['accel_m_s2']]
    rows, seen, rejected, duplicate_count, previous_time = [], set(), Counter(), 0, None
    for s in samples:
        if not isinstance(s,dict):
            rejected['malformed_sample'] += 1
            continue
        boot, identity, t=s.get('boot_id'),s.get('sample_id'),s.get('time_s')
        if boot != baseline['boot_id']:
            rejected['different_boot'] += 1
        elif s.get('frame') != baseline['frame']:
            rejected['different_frame'] += 1
        elif s.get('gyro_reference') != baseline.get('gyro_reference'):
            rejected['gyro_calibration_changed'] += 1
        elif identity is None or not isinstance(identity,(int,str)) or isinstance(identity,bool):
            rejected['missing_sample_identity'] += 1
        elif not _finite(t) or not _vec(s.get('accel_m_s2')) or not _vec(s.get('gyro_rad_s')):
            rejected['malformed_values'] += 1
        elif s.get('valid') is not True:
            rejected['invalid_sensor'] += 1
        elif (not _finite(s.get('age_upper_bound_s'))
              or not 0 <= s['age_upper_bound_s'] <= max_sample_age_s):
            rejected['stale_or_unbounded_age'] += 1
        elif (boot,identity) in seen:
            duplicate_count += 1
        elif previous_time is not None and t <= previous_time:
            rejected['nonmonotonic_sensor_time'] += 1
        else:
            seen.add((boot,identity)); rows.append(s); previous_time=t
    rates=[sum((g-b)*u for g,b,u in zip(s['gyro_rad_s'],baseline['gyro_rad_s'],up)) for s in rows]
    angle, covered, gaps, slopes = 0., 0., [], []
    for i in range(1,len(rows)):
        dt=rows[i]['time_s']-rows[i-1]['time_s']
        if dt > max_gap_s:
            gaps.append({'from_s':rows[i-1]['time_s'],'to_s':rows[i]['time_s'],'duration_s':dt})
            continue
        covered += dt
        angle += .5*(rates[i]+rates[i-1])*dt
        slopes.append(_norm([a-b for a,b in zip(rows[i]['accel_m_s2'],rows[i-1]['accel_m_s2'])])/dt)
    # Magnitude departure is orientation invariant, not gravity-free acceleration.
    jolts=[{'time_s':s['time_s'],'magnitude_departure_m_s2':abs(_norm(s['accel_m_s2'])-gravity)}
           for s in rows if abs(_norm(s['accel_m_s2'])-gravity)>=jolt_threshold_m_s2]
    reasons=[]
    if baseline.get('stationary_verified') is not True:reasons.append('stationary_baseline_unverified')
    if len(rows)<4:reasons.append('insufficient_samples')
    if gaps:reasons.append('unobserved_time_gaps')
    if rejected:reasons.append('rejected_samples')
    degrees=math.degrees(angle)
    return {'schema':2,'baseline_boot_id':baseline['boot_id'],'sensor_frame':baseline['frame'],
            'unique_samples':len(rows),'duplicate_samples':duplicate_count,
            'rejected_samples':dict(rejected),'quality_reasons':reasons,
            'observed_span_s':rows[-1]['time_s']-rows[0]['time_s'] if len(rows)>1 else 0.,
            'integrated_duration_s':covered,'unobserved_gaps':gaps,
            'quality':'observed_window' if not reasons else 'partial_or_provisional',
            'relative_up_imu':up,'observed_yaw_delta_deg':degrees,
            'window_yaw_delta_deg':degrees if not reasons else None,
            'rotation_candidate':abs(degrees)>=turn_threshold_deg,
            'rotation_sign':(1 if degrees>0 else -1) if abs(degrees)>=turn_threshold_deg else 0,
            'peak_corrected_yaw_rate_rad_s':max(map(abs,rates),default=0.),
            'peak_accel_change_rate_m_s3':max(slopes,default=0.),
            'jolt_candidates':jolts,'contact_confirmed':False,
            'clearance_verified':False,'metric_distance_available':False,
            'threshold_status':'provisional_not_hardware_qualified'}


def _sample(envelope):
    imu=envelope.get('ahrs',{}).get('imu',{})
    cache=envelope.get('cache_timing',{})
    age, elapsed, duration=imu.get('age_ms'),cache.get('elapsed_since_receive_s'),cache.get('request_duration_s')
    upper=(age/1000+elapsed+duration) if all(map(_finite,(age,elapsed,duration))) else None
    clock=imu.get('observed_uptime_ms')
    return {'boot_id':envelope.get('boot_id'),'sample_id':imu.get('sample_count'),
            'frame':imu.get('frame'),'time_s':clock/1000 if _finite(clock) else None,
            'accel_m_s2':imu.get('accel_m_s2'),'gyro_rad_s':imu.get('gyro_rad_s'),
            'valid':imu.get('valid'),'age_upper_bound_s':upper,
            'gyro_bias_applied':imu.get('gyro_bias_applied'),
            'gyro_reference':json.dumps([imu.get('gyro_bias_applied'),
                envelope.get('ahrs',{}).get('calibration',{}).get('gyro_bias_rad_s'),
                envelope.get('ahrs',{}).get('attitude',{}).get('generation')])}


def summarize_trial(trial, *, stationary_verified=False):
    """Adapt recorded telemetry. HTTP/camera/command timing is unqualified.

    Uptime wrap is unwrapped only within one unchanged boot and only for a
    forward modulo interval below half the clock range. Replays stay invalid.
    """
    baseline=_sample(trial['before']['ahrs'])
    baseline['stationary_verified']=stationary_verified
    rows=[]; last_raw=None; offset=0
    for obs in trial['observations']:
        s=_sample(obs['metadata'].get('ahrs',{})); raw=s['time_s']
        if s['boot_id']==baseline['boot_id'] and _finite(raw):
            if last_raw is not None and raw < last_raw:
                delta=(raw-last_raw) % (UINT32_MS/1000)
                if delta < UINT32_MS/2000:offset += UINT32_MS/1000
            last_raw=raw; s['time_s']=raw+offset
        rows.append(s)
    result=summarize_window(rows,baseline)
    result.update(command=trial['command'],baseline_provenance='pre-command firmware sample; stationarity must be independently verified',
                  gyro_bias_applied_in_firmware=baseline['gyro_bias_applied'],
                  timing_qualified=False,body_mounting_alignment_applied=False,
                  response_delay_s=None,response_delay_reason='command_to_sensor_clock_alignment_unqualified')
    return result


def build_response_model(trials):
    """Evidence ledger grouped by requested input, not a motor command model.

    Acknowledgment arrival delay is transport evidence, not physical response
    delay. No inference of distance, turn radius, heading compass or contact.
    """
    groups={}
    for reference,trial in trials:
        command=trial['command']; key=(command.get('direction'),command.get('steer'))
        group=groups.setdefault(key,{'direction':key[0],'steer':key[1],'cases':[]})
        try: signals=summarize_trial(trial)
        except (ValueError,TypeError,KeyError) as error:
            group['cases'].append({'reference':reference,'availability':'unavailable','reason':str(error)})
            continue
        accepted={e['accepted_sequence']:e['received'] for e in trial.get('events',[])
                  if e.get('type')=='accepted' and _finite(e.get('received'))}
        delays=[e['received']-accepted[e['intent_sequence']] for e in trial.get('events',[])
                if e.get('type')=='ack' and e.get('stage')=='applied'
                and e.get('intent_sequence') in accepted and _finite(e.get('received'))
                and e['received']>=accepted[e['intent_sequence']]]
        group['cases'].append({'reference':reference,'duration_requested_s':command.get('duration'),
            'signals':signals,'accepted_to_applied_ack_arrival_s':delays,
            'physical_response_delay_s':None,'commanded_input_is_observed_motion':False})
    return {'schema':1,'model_status':'evidence_ledger_not_fitted_controller',
            'coordinate_semantics':'signed rotation about stationary measured up; body left/right unqualified',
            'groups':list(groups.values()),'predictive_steering_model':None,
            'limits':['No qualified command-to-sensor clock mapping',
                      'Insufficient repeated complete stationary-baselined cases',
                      'No metric travel or turn-radius qualification',
                      'Absence of impact candidate does not establish clearance']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('trials',nargs='+',type=Path)
    parser.add_argument('--response-model',action='store_true')
    args=parser.parse_args()
    if args.response_model:
        trials=[(str(path),json.loads(path.read_text())) for path in args.trials]
        print(json.dumps(build_response_model(trials),allow_nan=False))
        return
    for path in args.trials:
        try:
            result=summarize_trial(json.loads(path.read_text()))
            print(json.dumps({'source':str(path),'signals':result},allow_nan=False))
        except (ValueError,KeyError,TypeError) as error:
            print(json.dumps({'source':str(path),'availability':'unavailable','reason':str(error)}))


if __name__=='__main__':main()
