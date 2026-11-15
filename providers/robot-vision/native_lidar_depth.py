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


def pnp_pair(ref, cur, ref_zones, cur_zones, stats=None):
    """Match two real image observations; cross-check depth in both captures."""
    stats={} if stats is None else stats
    if ref.des is None or cur.des is None or ref.depths is None or cur.depths is None:
        return
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(ref.des, cur.des, k=2)
    unique = {}
    for row in pairs:
        if len(row)==2 and row[0].distance < .75*row[1].distance:
            m = row[0]
            if m.trainIdx not in unique or unique[m.trainIdx].distance>m.distance:
                unique[m.trainIdx]=m
    pairs = list(unique.values())
    a = np.array([m.queryIdx for m in pairs], dtype=int)
    b = np.array([m.trainIdx for m in pairs], dtype=int)
    keep = (ref.depths[a]>0)&(cur.depths[b]>0)
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
    ii=ii[(predicted>0)&(np.abs(cur.depths[b[ii]]-predicted)<(.035+.065*predicted))]
    stats['depth_consistent_inliers']=len(ii)
    if len(ii)<18 or len(np.unique(cells[ii]))<4 or len(np.unique(sample(cur_zones,cur.kps[b[ii]],-1)))<4:
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

    def initialize(frame, image, *args, **kwargs):
        nonlocal previous
        obs=getattr(backend,'_native_scan',None)
        if obs is not None:
            stats=backend._lidar_stats
            stats['initializer_depth_frames']=stats.get('initializer_depth_frames',0)+1
            zones=obs['zones']
            if previous is not None and previous[0].img_id!=frame.img_id and previous[3]!=obs['scan_id']:
                ref,ref_image,ref_zones,_=previous
                stats['pair_attempts']=stats.get('pair_attempts',0)+1
                result=pnp_pair(ref,frame,ref_zones,zones,stats)
                if result is not None:
                    points,ai,bi,pose=result
                    ref.update_pose(np.eye(4));frame.update_pose(pose)
                    out=InitializerOutput()
                    out.kf_ref=KeyFrame(ref,ref_image);out.kf_cur=KeyFrame(frame,image)
                    out.idxs_ref=ai;out.idxs_cur=bi;out.pts=points
                    backend._native_birth={'source':'front_lidar_visual_pnp','initial_inliers':len(ai),
                        'reference_scan':previous[3],'current_scan':obs['scan_id'],
                        'source_frame':backend._source_frames.get(str(ref.img_id)),
                        'rig_revision':obs['rig_revision']}
                    enable_native_rgbd(runtime)
                    previous=None
                    return out,True
            if frame.depths is not None and np.count_nonzero(frame.depths>0)>=18:
                previous=(frame,image.copy(),zones,obs['scan_id'])
        return original(frame,image,*args,**kwargs)
    init.initialize=initialize
