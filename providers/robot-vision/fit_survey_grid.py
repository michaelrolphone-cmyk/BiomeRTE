import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import cv2
import numpy as np

from survey_grid_geometry import camera_matrix, corners, enhance, fit_fixed_center, grid_xyz

base, state = map(Path, sys.argv[1:3])
baseline = json.loads((base / "baseline.json").read_text())
cal = json.loads((base / "camera-calibration.json").read_text())
cam = np.asarray(baseline["reference"]["T_enu_camera"])
g = baseline["survey"]["calibration_grid"]
origin = np.asarray(g["derived_reference_xyz"]) * .0254
roi = [1040, 455, 210, 145]
db = sqlite3.connect("file:" + str(state / "observations/vision.sqlite3") + "?mode=ro", uri=True)
rows = db.execute("""select f.id,f.blob,f.sha256,f.payload,s.keyframe_id from frames f join sessions ss on ss.id=f.session_id
left join (select frame_id,json_extract(payload,'$.keyframe_id') as keyframe_id from snapshots where durable=1) s on s.frame_id=f.id
where ss.map_id=? and f.blob is not null and f.size_bytes>24500 order by f.sequence,f.id""",(baseline["initial_local_map_id"],)).fetchall()
if not rows:
    raise ValueError("no retained lit frames")
out = base / "visual-reference"
out.mkdir(exist_ok=True)
records = []
for i in np.unique(np.linspace(0, len(rows)-1, min(len(rows),24)).astype(int)):
    id, file, sha, payload, kf = rows[i]
    data = (state / "observations/frame_blobs" / file).read_bytes()
    img = cv2.imdecode(np.frombuffer(data,np.uint8),0)
    p = corners(img,roi)
    if p is None:
        continue
    if p[0,:,1].mean() > p[-1,:,1].mean(): p = p[::-1]
    if p[:,0,0].mean() > p[:,-1,0].mean(): p = p[:,::-1]
    digest = hashlib.sha256(data).hexdigest()
    if sha and digest != sha: raise ValueError("image hash mismatch")
    filename = id.replace(":","-") + ".jpg"
    (out / filename).write_bytes(data)
    records.append(dict(frame_id=id,file=filename,sha256=digest,native_keyframe=kf,
                        captured_at=json.loads(payload).get("receive_decode_time"),raw_corners_px=p.tolist()))
if len(records)<3: raise RuntimeError("insufficient independent full-board detections: "+str(len(records)))
ps = np.asarray([r["raw_corners_px"] for r in records])
p = np.median(ps[::2],axis=0)
hypotheses=[]
for size in sorted({g["black_square_side_mm"]*.001, g["grid_width_east_mm"]*.001/10}):
    world = grid_xyz(origin,size)
    for mirror in [False,True]:
        for lr in [False,True]:
            for ud in [False,True]:
                ordered=p[::(-1 if ud else 1),::(-1 if lr else 1)].reshape(-1,2)
                fit=fit_fixed_center(ordered,world,cam[:3,3],cal,mirror,cam[:3,:3])
                fit.update(square_m=size, columns_reversed=lr, rows_reversed=ud)
                hypotheses.append(fit)
# Use the nearly upright north-facing mount to disambiguate symmetric orientations.
near=[h for h in hypotheses if h["mount_delta_deg"] < 30]
if not near: raise RuntimeError("no physically compatible parity/orientation")
w=min(near,key=lambda h:h["rms_px"])
steps=(slice(None,None,-1 if w["rows_reversed"] else 1),slice(None,None,-1 if w["columns_reversed"] else 1))
world=grid_xyz(origin,w["square_m"])
cc=(world-cam[:3,3]) @ np.asarray(w["R_enu_camera"])
if w["image_x_reflected"]:cc[:,0]*=-1
pred=cv2.projectPoints(cc,np.zeros(3),np.zeros(3),camera_matrix(cal),np.array(cal["distortion"]))[0].reshape(-1,2)
err=[np.sqrt(np.mean(np.sum((pred-b[steps].reshape(-1,2))**2,axis=1))) for b in ps[1::2]]
meta=dict(schema="rover.survey.visual-grid/1",world_id=baseline["world_id"],calibration_id=cal["calibration_id"],roi=roi,
          intrinsics=cal,reference_map_id=baseline["initial_local_map_id"],pattern=[9,6],squares=[10,7],survey_origin_enu_m=origin.tolist(),camera_enu_m=cam[:3,3].tolist(),
          world_corners_m=world.tolist(),reference_corners_px=p[steps].reshape(-1,2).tolist(),fit=w,
          scale_alternatives=[min((h for h in near if h["square_m"]==size),key=lambda h:h["rms_px"]) for size in sorted({h["square_m"] for h in near})],
          heldout_rms_px=list(map(float,err)),train_frames=len(ps[::2]),holdout_frames=len(err),
          observations=records,reference_record=0,original_survey_extent_mm=[g["grid_width_east_mm"],g["grid_height_up_mm"]])
t=out/"grid.json.tmp"
t.write_text(json.dumps(meta,allow_nan=False))
t.replace(out/"grid.json")
print(json.dumps({k:v for k,v in meta.items() if k in ["fit","scale_alternatives","heldout_rms_px","train_frames","holdout_frames"]}))
