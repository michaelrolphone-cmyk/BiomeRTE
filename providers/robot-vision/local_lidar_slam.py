"""Survey-anchored local range SLAM, including scans with no tracked image."""
import json
import math
import time
from collections import defaultdict

import numpy as np
from scipy.spatial import cKDTree


def plane_normals(points, zones, sides, sensor_ids):
    """Estimate local tangent planes from adjacent returns on the same head."""
    xyz=np.asarray(points)
    normals=np.zeros_like(xyz)
    for i,(zone,side,sid) in enumerate(zip(zones,sides,sensor_ids)):
        ids=[j for j in range(len(xyz)) if sides[j]==side and sensor_ids[j]==sid
             and abs(zones[j]//side-zone//side)<=1 and abs(zones[j]%side-zone%side)<=1
             and np.linalg.norm(xyz[j]-xyz[i])<=.35]
        if len(ids)<4:continue
        patch=xyz[ids]-np.mean(xyz[ids],axis=0)
        _,values,vectors=np.linalg.svd(patch,full_matrices=False)
        if values[1]>.012 and values[2]<.22*values[1]:
            normals[i]=vectors[-1]
    return normals


def register_scan(xyz,pose,reference,normals,stationary=False):
    """Floor/support height and tilt come from navigation. Solve observable ENU x,y,yaw."""
    xyz=np.asarray(xyz,float);T=np.asarray(pose,float).copy()
    keep=np.linalg.norm(normals,axis=1)>.9
    if np.count_nonzero(keep)<6:return
    target=np.asarray(reference)[keep];normal=np.asarray(normals)[keep];tree=cKDTree(target)
    for step in range(1 if stationary else 6):
        rotated=xyz@T[:3,:3].T
        points=rotated+T[:3,3]
        distance,idx=tree.query(points)
        # Each previously observed patch gets one vote.
        nearest={}
        for i in np.argsort(distance):
            if distance[i]<.14:nearest.setdefault(int(idx[i]),i)
        if len(nearest)<8:return
        q=np.array(list(nearest.values()));j=idx[q];n=normal[j]
        r=np.sum(n*(points[q]-target[j]),axis=1)
        close=np.abs(r)<.08
        q=q[close];n=n[close];r=r[close]
        if len(q)<8:return
        jac=np.column_stack((n[:,:2],-n[:,0]*rotated[q,1]+n[:,1]*rotated[q,0]))
        weight=np.sqrt(np.minimum(1.,.025/np.maximum(np.abs(r),1e-7)))
        u,s,vt=np.linalg.svd(jac*weight[:,None],full_matrices=False)
        good=s>max(.25,s[0]*.05)
        if stationary:
            # A quiet IMU supplies a zero-motion constraint. Evaluate LiDAR residuals
            # at that pose so a geometrically similar surface cannot move the rover.
            break
        if not good.any():return
        dx=-vt[good].T@((u[:,good].T@(r*weight))/s[good])
        if np.linalg.norm(dx[:2])>.14 or abs(dx[2])>np.deg2rad(12):return
        a=dx[2];c=math.cos(a);v=math.sin(a)
        T[:3,:3]=np.array([[c,-v,0],[v,c,0],[0,0,1]])@T[:3,:3]
        T[:2,3]+=dx[:2]
        if np.linalg.norm(dx)<1e-4:break
    error=float(np.median(np.abs(r)))
    if error>.04:return
    return T,dict(inliers=len(q),observable_dof=int(good.sum()),median_plane_residual_m=error,stationary_pose_held=bool(stationary))


class LocalLidarSlam:
    def __init__(self, service, survey_seed):
        self.service=service
        self.seed=survey_seed
        self.state={}
        self.map_id=None
        self.last=0.
        self._bootstrap_map_id=None
        self._next_bootstrap=0.
        self.last_pose_publish=0.
        self.seen={}
        self.cells={}
        self.candidates=defaultdict(list)
        self.seeded=set()
        self.reference=None

    def bootstrap(self):
        if self.seed is None:return
        service=self.service
        map_id=service.map_id
        now=time.monotonic()
        if self._bootstrap_map_id==map_id and now<self._next_bootstrap:return
        # One attempt clock for the worker and packet paths. Record it before
        # I/O so empty results or errors cannot turn retries into per-packet work.
        self._bootstrap_map_id=map_id
        self._next_bootstrap=now+2.
        if self.map_id!=map_id:
            self.map_id=map_id;self.cells={};self.seeded=set();self.seen={};self.candidates.clear()
            self.reference=None;self.state={}
            cursor=None
            while True:
                page=service.store.read_map(map_id,kinds=['lidar_landmark'],cursor=cursor,limit=250)
                for item in page['entities']:
                    data=item['data'];self.cells[item['entity_id']]=data
                    if data.get('survey_seed'):self.seeded.add(item['entity_id'])
                cursor=page.get('next_cursor')
                if not cursor:break
        atlas=self.seed.atlas
        survey=json.loads((service.state_dir/'survey-world.json').read_text())
        with atlas.lock:
            rows=[(rid,json.loads(data)) for rid,data in atlas.db.execute("SELECT id,data FROM survey_lidar_landmarks")]
        rows=[(i,d) for i,d in rows if d['world_id']==survey['world_id'] and d['survey_revision']==survey['survey_revision']
              and d['rig_revision']==service.lidar.config['revision']]
        if not rows:return
        changes=[]
        if not self.cells:
            self.state["status"]="survey_seed"
        if any(i not in self.seeded for i,d in rows):
            normals=plane_normals([d['xyz'] for i,d in rows],[d['zone'] for i,d in rows],
                                  [math.isqrt(d['resolution']) for i,d in rows],[d['sensor_id'] for i,d in rows])
            for (i,d),normal in zip(rows,normals):
                if i in self.seeded:continue
                data=dict(d,survey_seed=True,source='local_lidar_slam',normal_enu=normal.tolist())
                changes.append(dict(kind='lidar_landmark',entity_id=i,op='upsert',data=data))
            with service._pipeline_lock:
                if service.map_id!=map_id:return
                service._commit_changes(changes,'local_lidar_survey_seed')
            for c in changes:self.cells[c['entity_id']]=c['data'];self.seeded.add(c['entity_id'])
        self.state.pop('error',None)
        self.reference=survey
        self.state.update(map_id=map_id,world_id=survey['world_id'],landmarks=len(self.cells),
                          survey_seed_points=len(self.seeded),coordinate_frame='survey_enu',units='m')

    def observe(self, rows, packet, interval, map_id):
        self.bootstrap()
        if self.map_id!=map_id or not self.cells:return
        current=self.seed.current(time.time())
        if not current:return
        nav,survey,zero,published=current
        pose=nav.get('world_body_pose') or {}
        at=time.time()-time.monotonic()+sum(interval)/2
        if (survey['world_id']!=self.reference['world_id'] or pose.get('world_id')!=survey['world_id']
                or abs(published-at)>1.5 or abs(pose.get('attitude_observed_at',0)-at)>1.):return
        R=np.asarray(pose.get('rotation_enu_body'),float);p=np.asarray(pose.get('position_enu_m'),float)
        if R.shape!=(3,3) or p.shape!=(3,) or not np.isfinite(R).all() or not np.isfinite(p).all():return
        Twb=np.eye(4);Twb[:3,:3]=R;Twb[:3,3]=p
        Tbc=np.linalg.solve(survey['reference']['T_enu_body'],survey['reference']['T_enu_camera'])
        xyz=[];zones=[];sides=[];names=[]
        for row in rows:
            sid=row['sensor_id'];raw=row['raw'];tick=raw.get('observed_monotonic_us')
            identity=(packet['receiver_id'],packet['boot_id'],tick)
            if tick is None or self.seen.get(sid)==identity or not row['points']:continue
            lag=(packet['capture_monotonic_us']-tick)/1e6
            if not 0<=lag<=.25:continue
            self.seen[sid]=identity
            M=Tbc@np.asarray(row['mount']['T_front_camera_sensor_m'])
            for pt in row['points']:
                xyz.append(M[:3,:3]@pt['xyz_sensor_m']+M[:3,3])
                zones.append(pt['zone']);sides.append(math.isqrt(raw['resolution']));names.append(sid)
        if len(xyz)<8:return
        self.last=time.monotonic()
        cells=list(self.cells.values())
        stationary=nav.get('state')=='stationary' and (nav.get('stationary_reference') or {}).get('imu_quiet') is True
        result=register_scan(xyz,Twb,[v['xyz'] for v in cells],[v['normal_enu'] for v in cells],stationary=stationary)
        if result is None:
            self.state.update(status='no_observable_scan_match',source_scan=packet['frame_id'])
            return
        T,fit=result
        self.state.update(status='tracking',**fit,T_enu_body=T.tolist(),heads=sorted(set(names)),source_scan=packet['frame_id'],
                          scans_matched=self.state.get('scans_matched',0)+1)
        if self.last-self.last_pose_publish>=1:
            sample=dict(self.state,observed_at=at,source='local_lidar_slam')
            with self.service._pipeline_lock:
                if self.service.map_id!=map_id:return
                self.service._commit_changes([dict(kind='lidar_pose',entity_id='scan-match',op='upsert',data=sample)],
                                             'lidar_scan_match')
            self.last_pose_publish=self.last
        # A rank-deficient fit can still correct observable axes. Admit new surfaces only
        # with a full planar pose or a verified stationary survey viewpoint.
        zero_witness=nav['state']=='stationary' and pose.get('survey_zero_id')==zero['id'] and np.linalg.norm(T[:3,3]-np.asarray(survey['reference']['T_enu_body'])[:3,3])<.025
        if fit['observable_dof']!=3 and not zero_witness:return
        cloud=np.asarray(xyz)@T[:3,:3].T+T[:3,3]
        tree=cKDTree([v['xyz'] for v in cells]);dist,_=tree.query(cloud);normal=plane_normals(cloud,zones,sides,names)
        changes=[]
        for i,x in enumerate(cloud):
            if dist[i]<.075 or np.linalg.norm(normal[i])<.9:continue
            key='surface:'+','.join(map(str,np.floor(x/.06).astype(int)))
            if key in self.cells:continue
            samples=self.candidates[key]
            if samples and samples[-1][0]==packet['frame_id']:continue
            samples.append((packet['frame_id'],at,x))
            samples[:]=[s for s in samples if at-s[1]<3][-5:]
            if len(samples)<4:continue
            data=dict(xyz=np.median([s[2] for s in samples],axis=0).tolist(),normal_enu=normal[i].tolist(),
                      coordinate_frame='survey_enu',world_id=survey['world_id'],units='m',source='local_lidar_slam',
                      sensor_id=names[i],zone=zones[i],observed_at=at,samples=len(samples),survey_seed=False)
            changes.append(dict(kind='lidar_landmark',entity_id=key,op='upsert',data=data))
        if changes:
            with self.service._pipeline_lock:
                if self.service.map_id!=map_id:return
                self.service._commit_changes(changes,'lidar_scan_to_map')
            self.cells.update((c['entity_id'],c['data']) for c in changes)
            self.state['landmarks']=len(self.cells)
        self.candidates=defaultdict(list,{k:s for k,s in self.candidates.items() if k not in self.cells and s and at-s[-1][1]<3})
