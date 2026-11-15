"""Sparse VL53L5CX depth for the native visual tracker and local mapper."""
import math

import cv2
import numpy as np


def intrinsics(cal):
    return np.array([[cal['fx'], 0, cal['cx']], [0, cal['fy'], cal['cy']], [0, 0, 1.]])


def project_depth(points, mount, side, cal, camera_from_sensor):
    """Axial depth only in the central part of each measured angular cell."""
    depth = np.zeros((cal['height'], cal['width']), np.float32)
    zones = np.full(depth.shape, -1, np.int16)
    tx, ty = [math.tan(math.radians(mount[k])/2)/side for k in
              ('fov_horizontal_deg', 'fov_vertical_deg')]
    transform = np.asarray(camera_from_sensor, dtype=float)
    matrix = intrinsics(cal)
    distortion = np.asarray(cal['distortion'], dtype=float)
    patches = []
    for point in points:
        xyz = np.asarray(point['xyz_sensor_m'])
        z = xyz[2]
        # Leave a border between adjacent returns, including at occluding edges.
        corners = xyz + np.array([[-tx,-ty,0], [tx,-ty,0], [tx,ty,0], [-tx,ty,0]])*.65*z
        corners = corners @ transform[:3,:3].T + transform[:3,3]
        if np.min(corners[:,2]) <= .01:
            continue
        pixels = cv2.projectPoints(corners, np.zeros(3), np.zeros(3), matrix, distortion)[0][:,0]
        if not np.isfinite(pixels).all() or np.abs(pixels).max()>1e5:
            continue
        patches.append((float(corners[:,2].mean()), int(point['zone']), np.int32(np.rint(pixels))))
    for z, zone, poly in sorted(patches, key=lambda p:-p[0]):
        cv2.fillConvexPoly(depth, poly, z)
        cv2.fillConvexPoly(zones, poly, zone)
    return depth, zones


def sample(raster, kps, missing=0):
    xy = np.floor(kps).astype(int)
    good = (xy[:,0]>=0)&(xy[:,1]>=0)&(xy[:,0]<raster.shape[1])&(xy[:,1]<raster.shape[0])
    out = np.full(len(kps), missing, dtype=raster.dtype)
    out[good] = raster[xy[good,1], xy[good,0]]
    return out


def flow_matches(ref, cur, ref_image, cur_image, indices):
    """Associate repeat detections using bidirectional image flow and descriptor agreement."""
    if ref_image is None or cur_image is None or not len(indices) or not len(cur.kps):
        return {}
    def grayscale(image):
        return image if image.ndim==2 else cv2.cvtColor(image,cv2.COLOR_BGR2GRAY)
    a,b=grayscale(ref_image),grayscale(cur_image)
    if a.shape!=b.shape:return {}
    before=np.asarray(ref.kps[indices],np.float32).reshape(-1,1,2)
    after,ok,_=cv2.calcOpticalFlowPyrLK(a,b,before,None)
    if after is None:return {}
    reverse,back,_=cv2.calcOpticalFlowPyrLK(b,a,after,None)
    if reverse is None:return {}
    good=ok.ravel().astype(bool)&back.ravel().astype(bool)
    good &= np.linalg.norm((reverse-before).reshape(-1,2),axis=1)<.75
    from scipy.spatial import cKDTree
    distances,detections=cKDTree(cur.kps).query(after.reshape(-1,2))
    good &= distances<2.
    accepted={}
    for j in np.flatnonzero(good):
        source=int(indices[j]);target=int(detections[j])
        bits=cv2.norm(ref.des[source],cur.des[target],cv2.NORM_HAMMING)
        if bits>.25*8*ref.des.shape[1]:continue
        rank=(float(distances[j]),bits)
        if target not in accepted or rank<accepted[target][0]:
            accepted[target]=(rank,source)
    return {target:source for target,(_,source) in accepted.items()}


def pnp_pair(ref, cur, ref_zones, cur_zones, stats=None, images=None):
    """Match two real image observations; cross-check depth in both captures."""
    stats={} if stats is None else stats
    if ref.des is None or cur.des is None or ref.depths is None:
        return
    measured=np.flatnonzero(ref.depths>0)
    if not len(measured) or len(cur.des)<2:return
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(ref.des[measured], cur.des, k=2)
    unique = {}
    for row in pairs:
        if len(row)==2 and row[0].distance < .75*row[1].distance:
            m=row[0]
            if m.trainIdx not in unique or unique[m.trainIdx][0]>m.distance:
                unique[m.trainIdx]=(m.distance,int(measured[m.queryIdx]))
    associations={target:source for target,(_,source) in unique.items()}
    if images is not None and len(associations)<60:
        flow=flow_matches(ref,cur,*images,measured)
        used=set(associations.values())
        for target,source in flow.items():
            if target not in associations and source not in used:
                associations[target]=source
                used.add(source)
        stats['flow_verified_matches']=len(associations)-len(unique)
    b=np.array(list(associations),dtype=int)
    a=np.array(list(associations.values()),dtype=int)
    keep = ref.depths[a]>0
    if cur_zones is not None and cur.depths is not None:keep &= cur.depths[b]>0
    a,b = a[keep],b[keep]
    stats['depth_matches']=len(a)
    if len(a)<18:
        return
    # Limit the leverage of a textured object inside one LiDAR zone.
    cells=sample(ref_zones, ref.kps[a], -1)
    indices=np.concatenate([np.flatnonzero(cells==c)[:24] for c in np.unique(cells)])
    a,b,cells=a[indices],b[indices],cells[indices]
    stats['matched_zones']=len(np.unique(cells))
    if len(np.unique(cells))<4 or len(a)<18:
        return
    xyz=np.column_stack((ref.kpsn[a],np.ones(len(a))))*ref.depths[a,None]
    ok,r,t,inliers=cv2.solvePnPRansac(xyz,cur.kpsu[b],cur.camera.K,None,iterationsCount=150,
                                     reprojectionError=2.5,confidence=.999,flags=cv2.SOLVEPNP_EPNP)
    if not ok or inliers is None:
        return
    ii=inliers.ravel()
    stats['pnp_inliers']=len(ii)
    R=cv2.Rodrigues(r)[0]
    predicted=(xyz[ii]@R.T+t.ravel())[:,2]
    verified=predicted>0
    if cur_zones is not None and cur.depths is not None:
        verified &= np.abs(cur.depths[b[ii]]-predicted)<(.035+.065*predicted)
    ii=ii[verified]
    stats['depth_consistent_inliers']=len(ii)
    minimum=18 if cur_zones is not None else 30
    if len(ii)<minimum or len(np.unique(cells[ii]))<4:
        return
    if cur_zones is not None and len(np.unique(sample(cur_zones,cur.kps[b[ii]],-1)))<4:
        return
    ok,r,t=cv2.solvePnP(xyz[ii],cur.kpsu[b[ii]],cur.camera.K,None,r,t,True,cv2.SOLVEPNP_ITERATIVE)
    if not ok:
        return
    T=np.eye(4);T[:3,:3]=cv2.Rodrigues(r)[0];T[:3,3]=t.ravel()
    return xyz[ii],a[ii],b[ii],T


def depth_camera(camera):
    # Equivalent RGB-D disparity baseline for pySLAM's depth residual model.
    camera.b=.05
    camera.bf=camera.fx*camera.b
    camera.depth_factor=1.
    camera.depth_threshold=4.


def enable_native_rgbd(runtime):
    from pyslam.io.dataset_types import SensorType
    runtime.sensor_type=SensorType.RGBD
    runtime.camera.sensor_type=SensorType.RGBD
    depth_camera(runtime.camera)


def install_initializer(backend, runtime):
    from pyslam.slam.initializer import InitializerOutput
    from pyslam.slam import KeyFrame
    init=runtime.tracking.initializer
    original=init.initialize
    previous=None

    def remember(frame,image,obs):
        nonlocal previous
        if obs is not None and frame.depths is not None and np.count_nonzero(frame.depths>0)>=18:
            previous=(frame,image.copy(),obs['zones'],obs['scan_id'],obs['rig_revision'],obs.get('survey_reference'))

    first=init.init
    def first_frame(frame,image,*args,**kwargs):
        remember(frame,image,getattr(backend,'_native_scan',None))
        return first(frame,image,*args,**kwargs)

    def initialize(frame, image, *args, **kwargs):
        nonlocal previous
        obs=getattr(backend,'_native_scan',None)
        stats=backend._lidar_stats
        if obs is not None:
            stats['initializer_depth_frames']=stats.get('initializer_depth_frames',0)+1
        if previous is not None and previous[0].img_id!=frame.img_id:
            ref,ref_image,ref_zones,ref_scan,rig_revision,survey_reference=previous
            stats['pair_attempts']=stats.get('pair_attempts',0)+1
            fresh_depth=obs is not None and obs['scan_id']!=ref_scan
            result=pnp_pair(ref,frame,ref_zones,obs['zones'] if fresh_depth else None,stats,(ref_image,image))
            if result is not None:
                points,ai,bi,pose=result
                ref.update_pose(np.eye(4));frame.update_pose(pose)
                out=InitializerOutput()
                out.kf_ref=KeyFrame(ref,ref_image);out.kf_cur=KeyFrame(frame,image)
                out.idxs_ref=ai;out.idxs_cur=bi;out.pts=points
                backend._native_birth={'source':'front_lidar_visual_pnp','initial_inliers':len(ai),
                    'reference_scan':ref_scan,'current_scan':obs['scan_id'] if fresh_depth else None,
                    'survey_reference':survey_reference,
                    'source_frame':backend._source_frames.get(str(ref.img_id)),'rig_revision':rig_revision}
                enable_native_rgbd(runtime)
                previous=None
                return out,True
        remember(frame,image,obs)
        return original(frame,image,*args,**kwargs)
    init.init=first_frame
    init.initialize=initialize
