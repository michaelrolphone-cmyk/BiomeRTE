"""Frame-clock attitude estimation and persistent, anchored sonar observations.

Transforms use column vectors: T_parent_child maps child coordinates to parent.
Sonar lengths/mounts are metres; native monocular translations need a scale.
Raw rays survive missing poses/scale and are reprojected, never placed at origin.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import sqlite3
import threading
import uuid
import itertools
from collections import deque
from pathlib import Path

import numpy as np


def estimated_rover_mounts():
    """Nominal chassis geometry; explicit estimates until measured mounts replace it.

    User dimensions: width 5.5in, length 7.25in, optical origin 1.75in
    behind front. Front sonar is user-reported 1in above the floor, 1.5in
    below the nominal camera optical center; other sensor heights are unknown.
    """
    return {name: {'origin_camera_m': origin, 'direction_camera': direction,
                   'beam_half_angle_deg': 7.5, 'qualification': 'chassis_estimate',
                   'provenance': 'user_chassis_dimensions_not_measured_sensor_mount',
                   'vertical_offset_status': 'user_reported_front_height' if name=='front' else 'assumed_zero',
                   'height_above_floor_m':.0254 if name=='front' else None}
            for name, origin, direction in (
                ('front', [0,.0381,.04445], [0,0,1]),
                ('rear', [0,0,-.1397], [0,0,-1]),
                ('left', [-.06985,0,-.047625], [-1,0,0]),
                ('right', [.06985,0,-.047625], [1,0,0]))}


def sonar_image_constraint(observation,mount,calibration):
    """Project the mounted sonar beam into the calibrated camera image.

    This is a range/beam association region, never pixelwise depth. In
    particular the one-inch-high front transducer is below the camera origin.
    """
    if not mount or not calibration.get('validated'):
        return None
    origin,direction=SonarObstacleMap._mount(mount)
    distance=observation['distance_m']
    endpoint=origin+distance*direction
    axis=np.array([1.,0,0]) if abs(direction[0])<.9 else np.array([0.,1,0])
    u=np.cross(direction,axis);u/=np.linalg.norm(u);v=np.cross(direction,u)
    radius=distance*math.tan(math.radians(mount.get('beam_half_angle_deg',7.5)))
    points=[endpoint]+[endpoint+radius*(math.cos(t)*u+math.sin(t)*v) for t in np.linspace(0,2*math.pi,16,endpoint=False)]
    distortion=list(calibration.get('distortion') or [0]*5)+[0]*5
    k1,k2,p1,p2,k3=distortion[:5]
    projected=[]
    for point in points:
        if point[2]<=1e-6:return None
        x,y=point[:2]/point[2];r2=x*x+y*y
        radial=1+k1*r2+k2*r2*r2+k3*r2*r2*r2
        xd=x*radial+2*p1*x*y+p2*(r2+2*x*x)
        yd=y*radial+p1*(r2+2*y*y)+2*p2*x*y
        projected.append([calibration['fx']*xd+calibration['cx'],calibration['fy']*yd+calibration['cy']])
    return {'sensor_id':observation['sensor_id'],'measurement_time_s':observation['time_s'],
            'range_m':distance,'endpoint_camera_m':endpoint.tolist(),
            'beam_center_px':projected[0],'beam_boundary_px':projected[1:],
            'interpretation':'sonar_beam_region_not_exact_center_pixel_depth'}


def rotation(value):
    r = np.asarray(value, dtype=float)
    if (r.shape != (3, 3) or not np.isfinite(r).all()
            or not np.allclose(r.T @ r, np.eye(3), atol=1e-5)
            or not np.isclose(np.linalg.det(r), 1, atol=1e-5)):
        raise ValueError('invalid_rotation')
    return r


def transform(value):
    t = np.asarray(value, dtype=float)
    if (t.shape != (4, 4) or not np.isfinite(t).all()
            or not np.allclose(t[3], [0, 0, 0, 1])):
        raise ValueError('invalid_transform')
    rotation(t[:3, :3])
    return t


def quaternion_rotation(value):
    q = np.asarray(value, dtype=float)
    if q.shape != (4,) or not np.isfinite(q).all() or abs(np.linalg.norm(q)-1) > .01:
        raise ValueError('invalid_quaternion')
    w, x, y, z = q / np.linalg.norm(q)
    return np.array([[1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
                     [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
                     [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)]])


def blend_rotation(prior, measured, weight):
    """Project a weighted orientation estimate back onto SO(3)."""
    u, _, vt = np.linalg.svd((1-weight)*prior + weight*measured)
    return u @ np.diag([1, 1, np.linalg.det(u @ vt)]) @ vt


def attitude_sample(frame, max_age_s=.15):
    packet = frame.get('sensor_packet') or {}
    raw = packet.get('ahrs') or {}
    state = raw.get('ahrs') or {}
    a, imu = state.get('attitude') or {}, state.get('imu') or {}
    if (raw.get('boot_id') != packet.get('boot_id')
            or raw.get('receiver_id') != packet.get('receiver_id')
            or not a.get('valid') or not imu.get('valid')):
        return None
    capture = packet.get('capture_monotonic_us')
    observed = imu.get('observed_uptime_ms')
    if type(capture) is not int or type(observed) is not int:
        return None
    age_ms = ((capture//1000)-observed) & 0xffffffff
    if age_ms > max_age_s*1000:
        return None
    try:
        r = quaternion_rotation(a.get('quaternion_wxyz'))
    except (ValueError, TypeError):
        return None
    return {'R': r, 'epoch': (raw['receiver_id'], raw['boot_id'],
                              a.get('generation'), a.get('yaw_origin_uptime_ms')),
            'time_s': capture/1e6-age_ms/1000, 'age_s': age_ms/1000}


class AttitudePoseEstimator:
    """Use mounted AHRS increments as a tracking prior and fuse visual attitude.

    Absolute yaw never overwrites the map heading. Re-anchor each accepted visual
    pose, so loop closure/relocalization is preserved. No accelerometer double
    integration or unmeasured translation is published as a localized pose.
    """
    def __init__(self, config=None):
        self.config = copy.deepcopy(config or {})
        mount = self.config.get('R_camera_imu')
        self.mount = None if mount is None else rotation(mount)
        self.mount_supplied = mount is not None
        self.mount_rank = 3 if mount is not None else 0
        self.observed_axis = None
        self.previous = None
        self.pending = None
        self.mount_observations = deque(maxlen=100)
        self.visual_anchor = None
        self.status = {'available': False, 'reason': 'imu_mount_not_configured'}

    def prior(self, frame, map_id):
        sample = attitude_sample(frame, self.config.get('max_attitude_age_s', .15))
        self.pending = (sample, map_id)
        if self.mount is None or sample is None:
            self.status = {'available': False, 'reason': 'imu_mount_not_configured' if self.mount is None else 'attitude_unavailable_or_stale'}
            return None
        old = self.previous
        if (old is None or old['map_id'] != map_id or old['sample']['epoch'] != sample['epoch']
                or not 0 < sample['time_s']-old['sample']['time_s'] <= self.config.get('max_prior_gap_s', .5)):
            self.status = {'available': False, 'reason': 'awaiting_visual_attitude_anchor'}
            return None
        increment = old['sample']['R'].T @ sample['R']
        if self.mount_rank == 1:
            vector = np.array([increment[2,1]-increment[1,2],
                               increment[0,2]-increment[2,0], increment[1,0]-increment[0,1]])
            length = np.linalg.norm(vector)
            if length > .01 and abs(float(vector @ self.observed_axis))/length < .98:
                self.status = {'available': False, 'reason': 'rotation_outside_calibrated_axis'}
                return None
        delta = self.mount @ increment @ self.mount.T
        prior = old['T'].copy()
        prior[:3, :3] = old['T'][:3, :3] @ delta
        self.status = {'available': True, 'reason': None, 'orientation_source': 'capture_clock_ahrs',
                       'translation_source': 'previous_visual_pose', 'translation_predicted': False,
                       'measurement_age_s': sample['age_s']}
        self.status.update(mount_source='configured' if self.mount_supplied else 'paired_visual_ahrs_rotations',
                           mount_observable_rank=self.mount_rank)
        return prior

    def fuse(self, localization, prior=None):
        result = copy.deepcopy(localization)
        sample, map_id = self.pending or (None, None)
        if not result.get('valid') or result.get('T_map_camera') is None:
            # Keep rotation evidence, but do not certify a stationary position
            # while the rover may be moving during loss of visual tracking.
            result['orientation_prior'] = None if prior is None else prior[:3, :3].tolist()
            result['sensor_fusion'] = dict(self.status, translation_observed=False)
            return result
        pose = transform(result['T_map_camera']).copy()
        if sample is not None:
            old = self.visual_anchor
            if old and old['map_id'] == map_id and old['sample']['epoch'] == sample['epoch']:
                a = old['sample']['R'].T @ sample['R']
                b = old['T'][:3, :3].T @ pose[:3, :3]
                # Rotation vectors are conjugate under the rigid IMU mount.
                def vector(r):
                    angle = math.acos(float(np.clip((np.trace(r)-1)/2, -1, 1)))
                    if not .015 < angle < .4:
                        return None
                    return angle/(2*math.sin(angle))*np.array([r[2,1]-r[1,2], r[0,2]-r[2,0], r[1,0]-r[0,1]])
                va, vb = vector(a), vector(b)
                if va is not None and vb is not None and abs(np.linalg.norm(va)-np.linalg.norm(vb)) < .035:
                    self.mount_observations.append((va, vb))
                if not self.mount_supplied and len(self.mount_observations) >= 8:
                    aa, bb = map(np.asarray, zip(*self.mount_observations))
                    u, singular, vt = np.linalg.svd(bb.T @ aa)
                    fit = u @ np.diag([1,1,np.linalg.det(u @ vt)]) @ vt
                    errors = np.linalg.norm(aa @ fit.T-bb, axis=1)
                    if np.percentile(errors, 90) < .025:
                        self.mount = fit
                        self.mount_rank = int(np.sum(singular > singular[0]*.03))
                        self.observed_axis = vt[0]
                        self.status = {'available': False, 'reason': 'mount_learned_awaiting_next_frame',
                                       'mount_source': 'paired_visual_ahrs_rotations',
                                       'mount_observable_rank': self.mount_rank}
            self.visual_anchor = {'sample': sample, 'map_id': map_id, 'T': pose.copy()}
        if prior is not None:
            delta = prior[:3, :3].T @ pose[:3, :3]
            angle = math.acos(float(np.clip((np.trace(delta)-1)/2, -1, 1)))
            # A large innovation can be relocalization/loop closure. Accept the
            # visual coordinate change and re-anchor instead of dragging it back.
            if angle <= math.radians(self.config.get('innovation_gate_deg', 25)):
                pose[:3, :3] = blend_rotation(prior[:3, :3], pose[:3, :3],
                    self.config.get('visual_orientation_weight', .8))
                result['sensor_fusion'] = dict(self.status, orientation_fused=True,
                                                innovation_rad=angle)
            else:
                result['sensor_fusion'] = dict(self.status, orientation_fused=False,
                                                reason='visual_reference_change', innovation_rad=angle)
        else:
            result['sensor_fusion'] = dict(self.status, orientation_fused=False)
        result['T_map_camera'] = pose.tolist()
        if sample is not None and self.mount is not None:
            self.previous = {'sample': sample, 'T': pose, 'map_id': map_id}
        return result


class PoseTimeline:
    """Interpolate poses at each sonar's measurement time, never HTTP receipt."""
    def __init__(self, max_gap_s=.5, retention_s=5):
        self.max_gap_s, self.retention_s = max_gap_s, retention_s
        self.poses = deque()
        self.context = None

    def append(self, map_id, boot_id, time_s, pose):
        context = (map_id, boot_id)
        if context != self.context:
            self.poses.clear()
            self.context = context
        if self.poses and time_s <= self.poses[-1][0]:
            return
        self.poses.append((time_s, transform(pose).copy()))
        while self.poses and time_s-self.poses[0][0] > self.retention_s:
            self.poses.popleft()

    def at(self, time_s, boot_id):
        if self.context is None or boot_id != self.context[1]:
            return None
        for t, pose in self.poses:
            if abs(t-time_s) < 1e-8:
                return pose.copy()
        for (t0, a), (t1, b) in zip(self.poses, list(self.poses)[1:]):
            if t0 <= time_s <= t1 and t1-t0 <= self.max_gap_s:
                weight = (time_s-t0)/(t1-t0)
                pose = np.eye(4)
                pose[:3,:3] = blend_rotation(a[:3,:3], b[:3,:3], weight)
                pose[:3,3] = (1-weight)*a[:3,3]+weight*b[:3,3]
                return pose
        return None


class SonarMetricOdometry:
    """Metric local pose from directional range changes and observed rotation.

    Small-step static-surface constraints are robustly checked across at least
    three transducers spanning both floor-plane axes. This is estimated planar
    sonar odometry, not a measured global pose. Missing/inconsistent constraints
    leave position unknown; they never mean the rover stayed at its old origin.
    """
    def __init__(self, path, mounts):
        self.path, self.mounts = Path(path), copy.deepcopy(mounts)
        self.timeline, self.orientations = PoseTimeline(), PoseTimeline()
        self.previous = None
        self.epoch = None
        self.local_frame_id = str(uuid.uuid4())
        self.status = {'valid': False, 'reason': 'awaiting_observed_rotation_and_ranges'}
        if self.path.exists():
            try:
                state = json.loads(self.path.read_text())
                self.previous, self.epoch = state['previous'], state['epoch']
                self.local_frame_id = state['local_frame_id']
                for row in state.get('poses', []):
                    self.timeline.append(self.local_frame_id,row[2],row[0],row[1])
            except (ValueError, KeyError, OSError):
                self.previous = None

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        state = {'previous':self.previous,'epoch':self.epoch,'local_frame_id':self.local_frame_id,
                 'poses':[(t,p.tolist(),self.timeline.context[1]) for t,p in list(self.timeline.poses)[-3:]]}
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state,allow_nan=False))
        temporary.replace(self.path)

    def update(self, observations, absolute_rotation, epoch, rotation_time_s):
        if absolute_rotation is None or len(observations) < 3:
            return {'valid': False, 'reason':'insufficient_rotation_or_ranges', 'local_frame_id':self.local_frame_id}
        epoch = json.dumps(epoch,sort_keys=True)
        if epoch != self.epoch:
            self.epoch, self.previous = epoch, None
            self.local_frame_id = str(uuid.uuid4())
            self.timeline, self.orientations = PoseTimeline(), PoseTimeline()
        boot = observations[0]['boot_id']
        orientation = np.eye(4)
        orientation[:3,:3] = rotation(absolute_rotation)
        self.orientations.append(epoch,boot,rotation_time_s,orientation)
        scan = {o['sensor_id']: o for o in observations if o['sensor_id'] in self.mounts}
        if len(scan) < 3:
            return {'valid':False,'reason':'insufficient_mounted_transducers','local_frame_id':self.local_frame_id}
        t = float(np.mean([o['time_s'] for o in scan.values()]))
        at_scan = self.orientations.at(t,boot)
        if at_scan is None:
            return {'valid':False,'reason':'awaiting_rotation_at_measurement_time','local_frame_id':self.local_frame_id}
        old = self.previous
        if old is not None:
            common = sorted(set(old['scan']) & set(scan))
            if len(common) < 3 or any(scan[k]['id']==old['scan'][k]['id'] for k in common):
                return {'valid':False,'reason':'awaiting_new_transducer_cycle','local_frame_id':self.local_frame_id}
            if not 0 < t-old['time_s'] <= 1.:
                self.previous = None
                self.local_frame_id = str(uuid.uuid4())
                self.timeline = PoseTimeline()
                return {'valid':False,'reason':'metric_odometry_gap_new_local_frame','local_frame_id':self.local_frame_id}
            anchor = np.asarray(old['R_reference'])
            current_r = anchor.T @ at_scan[:3,:3]
            old_pose = transform(old['pose'])
            old_velocity = np.asarray(old['velocity_m_s'])
            dt = t-old['time_s']
            rows, targets = [], []
            for name in common:
                before, after = old['scan'][name], scan[name]
                origin, direction = SonarObstacleMap._mount(self.mounts[name])
                old_r = np.asarray(old['sensor_rotations'][name])
                new_rotation = self.orientations.at(after['time_s'],boot)
                if new_rotation is None:
                    continue
                new_r = anchor.T @ new_rotation[:3,:3]
                normal = old_r @ direction
                previous_endpoint = (old_pose[:3,3] + old_velocity*(before['time_s']-old['time_s'])
                                     + old_r @ (origin+direction*before['distance_m']))
                fraction = (after['time_s']-t)/dt
                target = normal @ (previous_endpoint-new_r@(origin+direction*after['distance_m'])
                                   +old_pose[:3,3]*fraction)
                rows.append((1+fraction)*normal[[0,2]])
                targets.append(target)
            a, b = np.asarray(rows), np.asarray(targets)
            if len(a)<3 or np.linalg.matrix_rank(a,tol=1e-5)<2:
                return {'valid':False,'reason':'translation_unobservable','local_frame_id':self.local_frame_id}
            best = None
            for indices in itertools.combinations(range(len(a)),2):
                indices=list(indices)
                if np.linalg.matrix_rank(a[indices],tol=1e-5)<2:
                    continue
                candidate=np.linalg.lstsq(a[indices],b[indices],rcond=None)[0]
                errors=np.abs(a@candidate-b)
                inliers=errors<=.06
                if inliers.sum()<3 or np.linalg.matrix_rank(a[inliers],tol=1e-5)<2:
                    continue
                candidate=np.linalg.lstsq(a[inliers],b[inliers],rcond=None)[0]
                rms=float(np.sqrt(np.mean((a[inliers]@candidate-b[inliers])**2)))
                score=(int(inliers.sum()),-rms)
                if best is None or score>best[0]:best=(score,candidate,rms,inliers)
            if best is None:
                return {'valid':False,'reason':'range_surface_constraints_inconsistent','local_frame_id':self.local_frame_id}
            _, position, rms, inliers = best
            estimate=np.eye(4);estimate[:3,:3]=current_r;estimate[[0,2],3]=position
            velocity=(estimate[:3,3]-old_pose[:3,3])/dt
            covariance=(.02**2+rms**2)*np.linalg.inv(a[inliers].T@a[inliers])
            quality={'residual_rms_m':rms,'inlier_transducers':int(inliers.sum()),
                     'translation_covariance_xz':covariance.tolist()}
        else:
            anchor=at_scan[:3,:3]
            estimate=np.eye(4);velocity=np.zeros(3)
            quality={'origin':'first_observable_metric_scan','translation_covariance_xz':None}
        sensor_rotations={}
        for name,o in scan.items():
            r=self.orientations.at(o['time_s'],boot)
            if r is None:
                return {'valid':False,'reason':'awaiting_per_transducer_orientation','local_frame_id':self.local_frame_id}
            sensor_rotations[name]=(anchor.T@r[:3,:3]).tolist()
        self.previous={'scan':scan,'time_s':t,'pose':estimate.tolist(),'R_reference':anchor.tolist(),
                       'sensor_rotations':sensor_rotations,'velocity_m_s':velocity.tolist()}
        self.timeline.append(self.local_frame_id,boot,t,estimate)
        self.status={'valid':True,'T_local_metric_camera':estimate.tolist(),'time_s':t,
                     'local_frame_id':self.local_frame_id,'coordinate_frame':'local_metric','units':'m',
                     'source':'AHRS_or_visual_rotation_and_sonar_surface_constraints',
                     'qualification':'estimated_planar_sonar_odometry','velocity_m_s':velocity.tolist(),
                     'assumptions':['locally_static_corresponding_surfaces','locally_perpendicular_beam_surface_model',
                                    'floor_plane_translation','declared_sensor_mounts'],**quality}
        self._save()
        return dict(self.status)


def sonar_observations(frame, max_age_s=1.):
    """Select causal per-transducer measurements in the image's device boot."""
    packet = frame.get('sensor_packet') or {}
    envelope = packet.get('ultrasonic') or {}
    if any(envelope.get(k) != packet.get(k) for k in ('receiver_id', 'boot_id')):
        return []
    capture = packet.get('capture_monotonic_us')
    state = envelope.get('ultrasonic') or {}
    if type(capture) is not int or state.get('paused') or not state.get('worker_ready'):
        return []
    result = []
    for row in state.get('sensors', []):
        observed, distance = row.get('observed_uptime_ms'), row.get('distance_m')
        if (not row.get('valid') or row.get('status') != 'ok' or type(observed) is not int
                or type(distance) not in (float, int) or not math.isfinite(distance) or not .02 <= distance <= 4):
            continue
        observed_us = row.get('observed_monotonic_us')
        completed_us = row.get('completed_monotonic_us')
        if type(observed_us) is int:
            if (observed_us > capture or type(completed_us) is not int or completed_us > capture
                    or completed_us < observed_us):
                continue
            age_ms = (capture-observed_us)/1000
        else:
            age_ms = ((capture//1000)-observed) & 0xffffffff
        if age_ms > max_age_s*1000:
            continue
        identity = [envelope.get('receiver_id'), envelope.get('boot_id'), row.get('id'),
                    row.get('sequence'), observed]
        result.append({'id': hashlib.sha256(json.dumps(identity).encode()).hexdigest(),
                       'sensor_id': row['id'], 'distance_m': float(distance),
                       'time_s': capture/1e6-age_ms/1000, 'age_s': age_ms/1000,
                       'boot_id': envelope['boot_id'], 'receiver_id': envelope['receiver_id']})
    return result


class SonarObstacleMap:
    """Incremental SQLite ray ledger; no copies of the map at checkpoints.

    A return is a beam/cone constraint, not an exact surface point. Centerline
    endpoints are exported with beam uncertainty, and ray free-space is kept
    separate from certified traversability. Query reprojection follows anchors.
    """
    def __init__(self, path, mounts=None, resolution_m=.05):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.mounts = copy.deepcopy(mounts or {})
        self.resolution = float(resolution_m)
        if not math.isfinite(self.resolution) or self.resolution <= 0:
            raise ValueError('invalid_obstacle_resolution')
        for mount in self.mounts.values():
            self._mount(mount)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.RLock()
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS rays (id TEXT PRIMARY KEY, map_id TEXT NOT NULL, data TEXT NOT NULL)')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(rays)')}
        for name, kind in [('resolved', 'INTEGER NOT NULL DEFAULT 0'), ('metric_resolved','INTEGER NOT NULL DEFAULT 0'), ('observed_s', 'REAL NOT NULL DEFAULT 0')]:
            if name not in columns:
                self.db.execute('ALTER TABLE rays ADD COLUMN '+name+' '+kind)
        self.db.execute('CREATE INDEX IF NOT EXISTS rays_map ON rays(map_id)')
        self.db.execute('CREATE INDEX IF NOT EXISTS rays_pending ON rays(map_id,resolved,observed_s)')
        self.db.execute('CREATE TABLE IF NOT EXISTS alignment_pairs (id TEXT PRIMARY KEY,map_id TEXT NOT NULL,data TEXT NOT NULL)')
        self.db.execute('CREATE INDEX IF NOT EXISTS alignment_map ON alignment_pairs(map_id)')
        self.db.commit()

    @staticmethod
    def _mount(mount):
        origin = np.asarray(mount['origin_camera_m'], dtype=float)
        direction = np.asarray(mount['direction_camera'], dtype=float)
        if (origin.shape != (3,) or direction.shape != (3,) or not np.isfinite(origin).all()
                or not np.isfinite(direction).all() or np.linalg.norm(direction) < 1e-9):
            raise ValueError('invalid_sonar_mount')
        return origin, direction/np.linalg.norm(direction)

    def record(self, map_id, frame_id, observations, pose_at, keyframe=None, revision=0,
               metric_pose_at=None, local_frame_id=None):
        """pose_at(time_s, boot_id) must interpolate in the capture-clock epoch.

        Store unresolved observations too: an unavailable pose never erases data
        or silently fabricates an endpoint. keyframe contains T_map_keyframe.
        """
        inserted = 0
        with self.lock, self.db:
            for observation in observations:
                item = copy.deepcopy(observation)
                item.update(frame_id=frame_id, mount=self.mounts.get(item['sensor_id']))
                item['recorded_revision'] = revision
                if metric_pose_at is not None:
                    metric_pose=metric_pose_at(item['time_s'],item['boot_id'])
                    if metric_pose is not None:
                        item.update(T_local_metric_camera=transform(metric_pose).tolist(),local_frame_id=local_frame_id,
                                    resolved_revision=revision)
                timed_pose = pose_at(item['time_s'], item['boot_id'])
                if timed_pose is not None:
                    pose = transform(timed_pose)
                    item['T_map_camera'] = pose.tolist()
                    item['resolved_revision'] = revision
                    if keyframe is not None:
                        item['keyframe_id'] = str(keyframe['id'])
                        item['T_keyframe_camera'] = (np.linalg.inv(transform(keyframe['T_map_keyframe'])) @ pose).tolist()
                inserted += self.db.execute('INSERT OR IGNORE INTO rays(id,map_id,data,resolved,metric_resolved,observed_s) VALUES (?,?,?,?,?,?)',
                    (item['id'], map_id, json.dumps(item, allow_nan=False),
                     int(item.get('T_map_camera') is not None),int(item.get('T_local_metric_camera') is not None), item['time_s'])).rowcount
        return inserted

    def resolve_pending(self, map_id, pose_at, keyframe=None, revision=0, earliest_time=None,
                        metric_pose_at=None, local_frame_id=None):
        changed = 0
        with self.lock, self.db:
            rows = self.db.execute('SELECT id,data FROM rays WHERE map_id=? AND (resolved=0 OR metric_resolved=0) AND observed_s>=?',
                                   (map_id, -1e100 if earliest_time is None else earliest_time)).fetchall()
            for identifier, raw in rows:
                item = json.loads(raw)
                changed_item=False
                if metric_pose_at is not None and item.get('T_local_metric_camera') is None:
                    metric_pose=metric_pose_at(item['time_s'],item['boot_id'])
                    if metric_pose is not None:
                        item.update(T_local_metric_camera=transform(metric_pose).tolist(),
                                    local_frame_id=local_frame_id,resolved_revision=revision)
                        changed_item=True
                pose = None if item.get('T_map_camera') is not None else pose_at(item['time_s'], item['boot_id'])
                if pose is None and not changed_item:
                    continue
                if pose is not None:
                    pose = transform(pose)
                    item['T_map_camera'] = pose.tolist()
                    item['resolved_revision'] = revision
                    if keyframe is not None:
                        item['keyframe_id'] = str(keyframe['id'])
                        item['T_keyframe_camera'] = (np.linalg.inv(transform(keyframe['T_map_keyframe'])) @ pose).tolist()
                self.db.execute('UPDATE rays SET data=?,resolved=?,metric_resolved=? WHERE id=?',
                    (json.dumps(item, allow_nan=False),int(item.get('T_map_camera') is not None),
                     int(item.get('T_local_metric_camera') is not None),identifier))
                changed += 1
        return changed

    def summary(self, map_id, scale=None):
        with self.lock:
            counts = dict(self.db.execute('SELECT resolved,count(*) FROM rays WHERE map_id=? GROUP BY resolved', (map_id,)))
            metric_count=self.db.execute('SELECT count(*) FROM rays WHERE map_id=? AND metric_resolved=1',(map_id,)).fetchone()[0]
        return {'map_id': map_id, 'ray_count': sum(counts.values()),
                'posed_observations': counts.get(1,0), 'unposed_observations': counts.get(0,0),
                'metric_observations':metric_count,
                'availability': 'available' if metric_count or (counts.get(1,0) and scale) else 'awaiting_observed_pose',
                'mounts': copy.deepcopy(self.mounts), 'resolution_m': self.resolution,
                'query_operation': 'robot.map.obstacles', 'free_space_certified': False}

    def offer_alignment(self,map_id,identifier,local_frame_id,visual_pose,metric_pose,keyframe=None,revision=0):
        item={'local_frame_id':local_frame_id,'T_map_camera':transform(visual_pose).tolist(),
              'T_local_metric_camera':transform(metric_pose).tolist(),'revision':revision}
        if keyframe is not None:
            item.update(keyframe_id=str(keyframe['id']),
                T_keyframe_camera=(np.linalg.inv(transform(keyframe['T_map_keyframe']))@transform(visual_pose)).tolist())
        with self.lock,self.db:
            self.db.execute('INSERT OR IGNORE INTO alignment_pairs VALUES (?,?,?)',
                (identifier,map_id,json.dumps(item,allow_nan=False)))

    def alignment(self,map_id,local_frame_id,resolve_anchor=None,revision=None):
        with self.lock:
            rows=self.db.execute('SELECT data FROM alignment_pairs WHERE map_id=? ORDER BY rowid DESC LIMIT 128',(map_id,)).fetchall()
        pairs=[]
        for (raw,) in rows:
            item=json.loads(raw)
            if item['local_frame_id']!=local_frame_id or (revision is not None and item['revision']>revision):continue
            visual=transform(item['T_map_camera'])
            if item.get('keyframe_id') is not None:
                anchor=None if resolve_anchor is None else resolve_anchor(item['keyframe_id'])
                if anchor is None:continue
                visual=transform(anchor)@transform(item['T_keyframe_camera'])
            pairs.append((visual,transform(item['T_local_metric_camera'])))
        unavailable={'valid':False,'reason':'awaiting_metric_visual_trajectory_correspondence',
                     'local_frame_id':local_frame_id,'units':'m'}
        if len(pairs)<4:return unavailable
        # Orientation observations fix relative axes, including a straight drive
        # where translation-only Sim(3) would be underconstrained.
        u,_,vt=np.linalg.svd(sum(b[:3,:3]@a[:3,:3].T for a,b in pairs))
        r=u@np.diag([1,1,np.linalg.det(u@vt)])@vt
        a=np.asarray([v[:3,3] for v,m in pairs]);b=np.asarray([m[:3,3] for v,m in pairs])
        x=(a-a.mean(0))@r.T;y=b-b.mean(0)
        denominator=float(np.sum(x*x))
        if denominator<1e-10 or np.linalg.norm(np.ptp(b,axis=0))<.05:return unavailable
        scale=float(np.sum(x*y)/denominator)
        if scale<=0:return unavailable
        translation=b.mean(0)-scale*r@a.mean(0)
        errors=np.linalg.norm(a@(scale*r).T+translation-b,axis=1)
        if np.percentile(errors,90)>.08:return {**unavailable,'reason':'metric_visual_trajectories_inconsistent'}
        t=np.eye(4);t[:3,:3]=scale*r;t[:3,3]=translation
        return {'valid':True,'T_local_metric_map':t.tolist(),'scale_m_per_map_unit':scale,
                'local_frame_id':local_frame_id,'rms_m':float(np.sqrt(np.mean(errors**2))),
                'pose_pairs':len(pairs),'qualification':'estimated_sonar_visual_alignment',
                'source':'timestamp_matched_visual_and_sonar_metric_poses'}

    def read(self, map_id, *, scale_m_per_map_unit=None, resolve_anchor=None, revision=None):
        rays, cells, free_cells, unresolved = [], {}, {}, 0
        scale = scale_m_per_map_unit
        if scale is not None and (type(scale) not in (int, float) or not math.isfinite(scale) or scale <= 0):
            raise ValueError('invalid_metric_scale')
        with self.lock:
            rows = self.db.execute('SELECT data FROM rays WHERE map_id=? ORDER BY id', (map_id,)).fetchall()
        for (raw,) in rows:
            item = json.loads(raw)
            if revision is not None and item.get('recorded_revision', 0) > revision:
                continue
            if revision is not None and item.get('resolved_revision', 0) > revision:
                unresolved += 1
                continue
            if not item.get('mount'):
                unresolved += 1
                continue
            pose = item.get('T_map_camera')
            frame_name='map_metric'
            local_frame_id=None
            factor=scale
            if scale is None or pose is None:
                pose=item.get('T_local_metric_camera')
                factor=1.
                frame_name='local_metric'
                local_frame_id=item.get('local_frame_id')
            elif item.get('keyframe_id') is not None:
                anchor = None if resolve_anchor is None else resolve_anchor(item['keyframe_id'])
                if anchor is None:
                    unresolved += 1
                    continue
                pose = transform(anchor) @ transform(item['T_keyframe_camera'])
            if pose is None:
                unresolved += 1
                continue
            pose = transform(pose).copy()
            pose[:3, 3] *= factor
            origin, direction = self._mount(item['mount'])
            origin = pose[:3, :3] @ origin + pose[:3, 3]
            direction = pose[:3, :3] @ direction
            endpoint = origin + direction*item['distance_m']
            beam = float(item['mount'].get('beam_half_angle_deg', 7.5))
            ray = {**item, 'origin_m': origin.tolist(), 'direction': direction.tolist(),
                   'endpoint_m': endpoint.tolist(), 'beam_half_angle_deg': beam,
                   'lateral_uncertainty_m': math.tan(math.radians(beam))*item['distance_m'],
                   'units': 'm', 'coordinate_frame': frame_name,'local_frame_id':local_frame_id}
            rays.append(ray)
            key = (frame_name,local_frame_id,*np.floor(endpoint/self.resolution).astype(int))
            cell = cells.setdefault(key, {'hits': 0, 'sensor_ids': set()})
            cell['hits'] += 1
            cell['sensor_ids'].add(item['sensor_id'])
            # Evidence along the beam centerline only. The broad ultrasound
            # return does not certify the whole cone or vehicle clearance.
            endpoint_guard = max(self.resolution, .03)
            for distance in np.arange(0, max(0,item['distance_m']-endpoint_guard), self.resolution*.5):
                free_key = (frame_name,local_frame_id,*np.floor((origin+direction*distance)/self.resolution).astype(int))
                free_cells.setdefault(free_key, set()).add(item['id'])
        return {'map_id': map_id, 'rays': rays,
                'obstacles': [{'voxel': [int(v) for v in k[2:]], 'center_m': ((np.asarray(k[2:])+.5)*self.resolution).tolist(),
                               'coordinate_frame':k[0],'local_frame_id':k[1],
                               'hits': v['hits'], 'sensor_ids': sorted(v['sensor_ids']),
                               'centerline_clear_observations': len(free_cells.get(k, ())),
                               'log_odds': float(np.clip(.85*v['hits']-.4*len(free_cells.get(k, ())), -5, 5))}
                              for k, v in cells.items()],
                'resolution_m': self.resolution, 'unresolved_observations': unresolved,
                'centerline_free_evidence_voxels': len(free_cells),
                'availability': 'available' if rays else 'awaiting_observed_pose',
                'representation': 'sonar_beam_centerline_evidence',
                'free_space_certified': False, 'traversability': 'not_certified'}

    def close(self):
        self.db.close()
