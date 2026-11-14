"""Surveyed checkerboard measurements in native (unmirrored-by-UI) images."""
import cv2
import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation


def camera_matrix(cal):
    return np.array([[cal["fx"], 0, cal["cx"]], [0, cal["fy"], cal["cy"]], [0, 0, 1.]], float)


def enhance(gray):
    f = gray.astype(np.float32)
    return np.clip(125 + 6 * (f - cv2.GaussianBlur(f, (0, 0), 9)), 0, 255).astype(np.uint8)


def corners(gray, roi):
    x, y, w, h = map(int, roi)
    patch = enhance(gray[y:y+h, x:x+w])
    if patch.size < 100 or patch.std() < 4:
        return None
    scale = 2
    big = cv2.resize(patch, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    ok, p = cv2.findChessboardCornersSB(big, (9, 6), cv2.CALIB_CB_NORMALIZE_IMAGE | cv2.CALIB_CB_ACCURACY)
    if not ok:
        return None
    return ((p.reshape(6, 9, 2) + .5) / scale - .5 + [x, y]).astype(np.float64)


def grid_xyz(origin, square):
    col, row = np.meshgrid(np.arange(1, 10), np.arange(1, 7))
    return np.asarray(origin) + np.stack([col, np.zeros_like(col), row], axis=-1).reshape(-1, 3) * square


def fit_fixed_center(pixels, world, camera, cal, reflected, nominal):
    K = camera_matrix(cal)
    D = np.array(cal["distortion"])
    rays = cv2.undistortPoints(pixels.reshape(-1, 1, 2), K, D).reshape(-1, 2)
    parity = -1 if reflected else 1
    directions = np.column_stack((parity * rays[:, 0], rays[:, 1], np.ones(len(rays))))
    target = world - camera
    initial = Rotation.align_vectors(target / np.linalg.norm(target, axis=1)[:, None],
                                     directions / np.linalg.norm(directions, axis=1)[:, None])[0]
    def residual(rot):
        cam = target @ Rotation.from_rotvec(rot).as_matrix()
        cam[:, 0] *= parity
        projected, _ = cv2.projectPoints(cam, np.zeros(3), np.zeros(3), K, D)
        return (projected.reshape(-1, 2) - pixels).ravel()
    fit = least_squares(residual, initial.as_rotvec(), max_nfev=45)
    rotation = Rotation.from_rotvec(fit.x)
    delta = Rotation.from_matrix(nominal.T @ rotation.as_matrix()).magnitude()
    return dict(rms_px=float(np.sqrt(np.mean(np.sum(fit.fun.reshape(-1, 2)**2, axis=1)))),
                mount_delta_deg=float(np.degrees(delta)), image_x_reflected=reflected,
                R_enu_camera=rotation.as_matrix().tolist())


def partial_board(reference_gray, reference_pixels, live_gray, expected):
    # Each corner retains its original row and column through occlusion.
    ref = reference_pixels.astype(np.float32).reshape(-1,1,2)
    guess = expected.astype(np.float32).reshape(-1,1,2)
    a, b = enhance(reference_gray), enhance(live_gray)
    options = dict(winSize=(11,11),maxLevel=1,criteria=(cv2.TERM_CRITERIA_COUNT|cv2.TERM_CRITERIA_EPS,25,.02))
    f, ok, _ = cv2.calcOpticalFlowPyrLK(a,b,ref,guess.copy(),flags=cv2.OPTFLOW_USE_INITIAL_FLOW,**options)
    back, ok2, _ = cv2.calcOpticalFlowPyrLK(b,a,f,None,**options)
    if back is None: return None
    valid = (ok.ravel()!=0) & (ok2.ravel()!=0) & (np.linalg.norm(back-ref,axis=2).ravel()<.85)
    valid &= np.linalg.norm(f-guess,axis=2).ravel()<3.
    for i in np.flatnonzero(valid):
        pa=cv2.getRectSubPix(a,(9,9),tuple(ref[i,0])).astype(float)
        pb=cv2.getRectSubPix(b,(9,9),tuple(f[i,0])).astype(float)
        pa-=pa.mean(); pb-=pb.mean()
        denom=np.linalg.norm(pa)*np.linalg.norm(pb)
        valid[i]=denom>180 and np.sum(pa*pb)/denom>.74
    ids=np.flatnonzero(valid)
    if len(ids)<12: return None
    _, mask=cv2.findHomography(ref[ids], f[ids],cv2.RANSAC,1.1)
    if mask is None: return None
    ids=ids[mask.ravel().astype(bool)]
    if len(ids)<12 or len(set(ids//9))<3 or len(set(ids%9))<4: return None
    return ids,f[ids].reshape(-1,2)


def camera_pose(data, observed, ids, cal):
    # Calibration is in raw pixels. Reverse optical parity after undistortion.
    K = camera_matrix(cal)
    xy = cv2.undistortPoints(observed.reshape(-1,1,2),K,np.array(cal["distortion"])).reshape(-1,2)
    if data["fit"]["image_x_reflected"]: xy[:,0] *= -1
    grid=np.asarray(data["world_corners_m"])[ids]
    rr=cv2.solvePnPGeneric(grid,xy,np.eye(3),None,flags=cv2.SOLVEPNP_IPPE)
    candidates=[]
    for rv,tv in zip(rr[1],rr[2]):
        R=cv2.Rodrigues(rv)[0]; center=(-R.T@tv).ravel()
        pts=(grid-center)@R.T
        if np.min(pts[:,2])<=0 or center[1]>=grid[0,1]: continue
        if data["fit"]["image_x_reflected"]: pts[:,0]*=-1
        uv=cv2.projectPoints(pts,np.zeros(3),np.zeros(3),K,np.array(cal["distortion"]))[0].reshape(-1,2)
        error=float(np.sqrt(np.mean(np.sum((uv-observed)**2,axis=1))))
        candidates.append(dict(camera_enu_m=center.tolist(),R_enu_camera=R.T.tolist(),rms_px=error))
    return sorted(candidates,key=lambda c:c["rms_px"])
