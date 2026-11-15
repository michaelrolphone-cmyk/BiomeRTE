"""Stationary, metric LiDAR landmarks in the surveyed world atlas."""
import json
import time
from collections import defaultdict, deque

import numpy as np


class SurveyLidarLandmarks:
    def __init__(self, directory, atlas):
        self.directory = directory
        self.atlas = atlas
        self.key = None
        self.seen = {}
        self.samples = defaultdict(lambda: deque(maxlen=13))
        self.finished = set()
        self.last_snapshot = 0.
        self.snapshot = None
        self.last_scan = None
        self.depths = {}
        self.reconfirmed = {}
        self.status = {}
        with atlas.lock, atlas.db:
            atlas.db.execute("""CREATE TABLE IF NOT EXISTS survey_lidar_landmarks
                (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, data TEXT NOT NULL)""")

    def current(self, wall):
        if wall-self.last_snapshot > .2 or self.snapshot is None:
            self.last_snapshot = wall
            try:
                state = json.loads((self.directory.parent/'biome-tof'/'latest.json').read_text())
                nav = state['inertial_navigation']
                survey = json.loads((self.directory/'survey-world.json').read_text())
                zero = json.loads((self.directory/'survey-pose-zero.json').read_text())
                self.snapshot = (nav,survey,zero,state['updated_at'])
            except (OSError,ValueError,KeyError):
                self.snapshot = None
        return self.snapshot

    def observe(self, rows, packet, interval, rig_revision):
        self.last_scan=None
        wall = time.time()
        snapshot = self.current(wall)
        if not snapshot or len(interval)!=2:
            return
        nav,survey,zero,published = snapshot
        pose = nav.get('world_body_pose') or {}
        if (survey['world_id'] != self.atlas.world_id or pose.get('world_id') != self.atlas.world_id
                or pose.get('survey_zero_id') != zero['id'] or zero['world_id'] != self.atlas.world_id):
            return
        transform = np.asarray(survey['reference']['T_enu_body'])
        live = np.asarray(pose.get('position_enu_m'), dtype=float)
        rotation = np.asarray(pose.get('rotation_enu_body'), dtype=float)
        if (nav.get('state')!='stationary' or not (nav.get('stationary_reference') or {}).get('imu_quiet')
                or live.shape!=(3,) or rotation.shape!=(3,3) or np.linalg.norm(live-transform[:3,3])>.015
                or np.trace(rotation.T@transform[:3,:3]) < 1+2*np.cos(np.deg2rad(3))
                or not 0<=wall-published<2.5):
            self.samples.clear()
            self.reconfirmed.clear()
            return
        key=(self.atlas.world_id,survey['survey_revision'],rig_revision,zero['id'])
        if self.key!=key:
            self.key=key
            self.samples.clear()
            self.seen.clear()
            self.reconfirmed.clear()
            with self.atlas.lock:
                self.depths={i:json.loads(d)['range_m'] for i,d in self.atlas.db.execute("SELECT id,data FROM survey_lidar_landmarks")}
                self.finished=set(self.depths)
        camera=np.asarray(survey['reference']['T_enu_camera'])
        capture=packet['capture_monotonic_us']
        stamp=wall-time.monotonic()+sum(interval)/2
        if interval[1]<interval[0] or interval[1]-interval[0]>1.:
            return
        additions=[]
        eligible=set()
        for row in rows:
            sid=row['sensor_id']
            raw=row['raw']
            us=raw.get('observed_monotonic_us')
            if not isinstance(us,int) or us>capture:continue
            at=stamp-(capture-us)/1e6
            if (not zero['at']<=at<=wall+.05 or abs(at-pose.get('attitude_observed_at',0))>1.5
                    or not 0<=wall-at<2.):continue
            identity=(packet['receiver_id'],packet['boot_id'],us)
            if self.seen.get(sid)==identity:continue
            self.seen[sid]=identity
            eligible.add(sid)
            sensor=camera@np.asarray(row['mount']['T_front_camera_sensor_m'])
            for p in row['points']:
                # One immutable landmark for each calibrated cell at this surveyed viewpoint.
                entity=f"survey-lidar:{key[1]}:{rig_revision}:{sid}:{raw['resolution']}:{p['zone']}"
                if entity in self.finished:
                    measured = float(p['depth_m'])
                    saved = self.depths[entity]
                    if np.isfinite(measured) and abs(measured - saved) <= max(.035, .04 * saved):
                        self.reconfirmed[entity] = (zero['id'], at)
                    else:
                        self.reconfirmed.pop(entity, None)
                    continue
                history=self.samples[entity]
                if history and at<=history[-1][0]:continue
                history.append((at,p['xyz_sensor_m']))
                recent=[point for t,point in history if at-t<=8]
                if len(recent)<7:continue
                cloud=np.asarray(recent);center=np.median(cloud,axis=0)
                error=np.linalg.norm(cloud-center,axis=1)
                inliers=error<=max(.025,.025*center[2])
                if sum(inliers)<max(6,.75*len(cloud)):continue
                center=np.median(cloud[inliers],axis=0)
                xyz=sensor[:3,:3]@center+sensor[:3,3]
                data=dict(xyz=xyz.tolist(),coordinate_frame='survey_enu',units='m',world_id=key[0],
                          source='survey_lidar',sensor_id=sid,zone=p['zone'],resolution=raw['resolution'],
                          rig_revision=rig_revision,survey_revision=key[1],zero_id=zero['id'],samples=int(sum(inliers)),
                          observed_at=at,range_m=float(center[2]),median_residual_m=float(np.median(error[inliers])),
                          mount_qualification=row['mount'].get('qualification'),receiver_id=packet['receiver_id'],
                          field_of_view_deg=[row['mount']['fov_horizontal_deg'],row['mount']['fov_vertical_deg']],
                          distance_semantics=row['mount']['distance_semantics'],rgb=[40,220,220])
                additions.append((entity,data))
        if additions:
            atlas=self.atlas
            with atlas.lock,atlas.db:
                revision=atlas.db.execute('SELECT revision FROM world WHERE id=?',(key[0],)).fetchone()[0]+1
                added=0
                for entity,data in additions:
                    added+=atlas.db.execute('INSERT OR IGNORE INTO survey_lidar_landmarks VALUES(?,?,?)',
                                         (entity,revision,json.dumps(data,allow_nan=False))).rowcount
                if added:atlas.db.execute('UPDATE world SET revision=? WHERE id=?',(revision,key[0]))
            self.finished.update(entity for entity,_ in additions)
        self.depths.update((i,d["range_m"]) for i,d in additions)
        self.last_scan=(packet["frame_id"],eligible)
        self.status={'world_id':key[0],'landmarks':len(self.finished),'zero_id':zero['id']}

    def depth_at_verified_start(self,sensor,rig_revision,frame_interval):
        """Return the saved survey depth only for a currently witnessed revisit to this zero."""
        wall=time.time()
        snapshot=self.current(wall)
        if not snapshot or frame_interval is None or len(frame_interval)!=2:return
        nav,survey,zero,published=snapshot
        pose=nav.get('world_body_pose') or {}
        t=wall-time.monotonic()+sum(frame_interval)/2
        if not (0<=wall-published<2.5 and 0<=wall-t<1.5 and t>=zero['at']
                and nav.get('state')=='stationary' and (nav.get('stationary_reference') or {}).get('imu_quiet')
                and pose.get('world_id')==survey['world_id']==zero['world_id']==self.atlas.world_id
                and pose.get('survey_zero_id')==zero['id'] and abs(t-pose.get('attitude_observed_at',0))<1.5):return
        known=np.asarray(survey['reference']['T_enu_body'])
        p=np.asarray(pose.get('position_enu_m'),float);R=np.asarray(pose.get('rotation_enu_body'),float)
        if p.shape!=(3,) or R.shape!=(3,3) or np.linalg.norm(p-known[:3,3])>.015 or np.trace(R.T@known[:3,:3])<1+2*np.cos(np.deg2rad(3)):return
        prefix=f"survey-lidar:{survey['survey_revision']}:{rig_revision}:{sensor}:"
        with self.atlas.lock:
            records=[(i,json.loads(v)) for i,v in self.atlas.db.execute(
                "SELECT id,data FROM survey_lidar_landmarks WHERE id LIKE ?",(prefix+'%',))]
        if not records:return
        rows=[]
        for entity,row in records:
            if row.get('samples',0)<6:continue
            seen_zero,seen_at=self.reconfirmed.get(entity,(None,0.))
            if row.get('zero_id')==zero['id'] or (seen_zero==zero['id'] and abs(t-seen_at)<=1.):
                rows.append(row)
        if len(rows)<4:return
        return rows,dict(world_id=survey['world_id'],zero_id=zero['id'],survey_revision=survey['survey_revision'],
                         T_enu_map=survey['reference']['T_enu_camera'])

    def visual_depths(self, frame_id, row):
        """Independent repeat observations at the known viewpoint constrain native map scale."""
        if not self.last_scan or self.last_scan[0]!=frame_id or row['sensor_id'] not in self.last_scan[1]:
            return row['points'],0
        cloud=[]
        count=0
        for p in row['points']:
            idx=f"survey-lidar:{self.key[1]}:{self.key[2]}:{row['sensor_id']}:{row['raw']['resolution']}:{p['zone']}"
            seed=self.depths.get(idx)
            if seed is None:
                cloud.append(p)
            elif abs(p['depth_m']-seed)<=max(.035,.04*seed):
                cloud.append(dict(p,depth_m=(p['depth_m']+seed)/2.))
                count+=1
            # A surface that moved since survey does not supply a native correspondence.
        return cloud,count

    def page(self, revision, after, limit):
        with self.atlas.lock:
            rows=self.atlas.db.execute("""SELECT id,data FROM survey_lidar_landmarks
                WHERE revision<=? AND id>? ORDER BY id LIMIT ?""",(revision,after,limit+1)).fetchall()
        return ([dict(kind='landmark',entity_id=key,data=json.loads(value)) for key,value in rows[:limit]],
                len(rows)>limit)
