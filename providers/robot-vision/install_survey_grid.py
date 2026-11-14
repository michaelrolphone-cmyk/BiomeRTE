"""Bind image observations to an existing survey without changing its measured extents."""
import json
import sqlite3
import sys
from pathlib import Path

import cv2
import numpy as np

from survey_grid_matcher import SurveyGridMatcher
from vision_config import atomic_json

def stationary_archive_match(image, reference, pixels):
    """Match labeled patches when both exposures belong to the surveyed fixed pose."""
    from survey_grid_geometry import enhance
    gray = enhance(image)
    ids, observed = [], []
    for i, pt in enumerate(pixels.astype(np.float32)):
        patch = cv2.getRectSubPix(reference, (13,13), tuple(pt))
        search = cv2.getRectSubPix(gray, (19,19), tuple(pt))
        corr = cv2.matchTemplate(search, patch, cv2.TM_CCOEFF_NORMED)
        _, score, _, maximum = cv2.minMaxLoc(corr)
        delta = np.asarray(maximum)-3
        if score > .77 and np.linalg.norm(delta) <= 2.25 and patch.std()>5:
            ids.append(i)
            observed.append(pt+delta)
    if len(ids) < 8 or len({i//9 for i in ids}) < 3 or len({i%9 for i in ids}) < 3:
        return {"status":"grid_unresolved_at_survey_pose","landmark_count":0}
    diff=np.asarray(observed)-pixels[ids]
    shift=np.median(diff,axis=0)
    good=np.linalg.norm(diff-shift,axis=1)<1.8
    ids=np.asarray(ids)[good]
    if len(ids)<8 or len(set(ids//9))<3 or len(set(ids%9))<3:
        return {"status":"grid_unresolved_at_survey_pose","landmark_count":0}
    return dict(status="survey_grid_matched",method="survey_pose_patch_correspondence",landmark_count=len(ids),
                landmark_indices=ids.tolist(),raw_corners_px=np.asarray(observed)[good].round(3).tolist(),
                median_patch_offset_px=shift.tolist())


folder, state = map(Path,sys.argv[1:3])
target=folder/"visual-reference"
meta=json.loads((target/"grid.json").read_text())
matcher=SurveyGridMatcher(target,meta["world_id"],meta["calibration_id"])
def sample(f):
    result=matcher.match(cv2.imread(str(f),cv2.IMREAD_GRAYSCALE))
    return dict(result,file=f.name)
from survey_grid_geometry import enhance
camera_images = [cv2.imread(str(target/r["file"]),0) for r in meta["observations"][::2]]
template = enhance(np.median(camera_images,axis=0).astype(np.uint8))
pixels = np.asarray(meta["reference_corners_px"])
old = []
for f in sorted(folder.glob("*.jpg")):
    observation=stationary_archive_match(cv2.imread(str(f),0),template,pixels)
    old.append(dict(observation,file=f.name))
held=[sample(target/r["file"]) for r in meta["observations"][1::2]]
db=sqlite3.connect("file:"+str(state/"observations/vision.sqlite3")+"?mode=ro",uri=True)
dark=[]
for fid,p in db.execute("select id,blob from frames where blob is not null and size_bytes between 17000 and 21800 order by rowid desc limit 25").fetchall()[::4]:
    d=sample(state/"observations/frame_blobs"/p);d["frame_id"]=fid;dark.append(d)
brief=lambda rows:[(r["status"],r.get("landmark_count",0)) for r in rows]
print("original",brief(old),"held",brief(held),"recent",brief(dark),flush=True)
if not any(r["status"]=="survey_grid_matched" for r in old) or not all(r["status"]=="survey_grid_matched" for r in held):
    raise ValueError("archive recognition still incomplete")
atomic_json(target/"verified-observations.json",dict(original=old,heldout=held,recent=dark))
ref={"schema":meta["schema"],"calibration_id":meta["calibration_id"],"landmark_count":54,
     "reference_map_id":meta["reference_map_id"],"training_frames":meta["train_frames"],
     "validation_frames":len(held),"archive_frames_with_matches":sum(r["status"]=="survey_grid_matched" for r in old),
     "fit":meta["fit"],"scale_alternatives":meta["scale_alternatives"],"holdout_median_rms_px":float(np.median(meta["heldout_rms_px"])),
     "landmarks_enu_m":meta["world_corners_m"],"archive":"visual-reference/grid.json",
     "dimension_status":"owner_confirmed_10_by_7_at_20mm",
     "scale_authority":"surveyed_target_only","native_map_scale":"unobserved"}
# The capture-time AHRS and sonar baseline remains immutable. The active
# surveyed world receives the corrected target extent and visual facts.
for path in (state/"survey-world.json",):
    data=json.loads(path.read_text())
    if data["world_id"]!=meta["world_id"]: raise RuntimeError("world changed")
    grid = data["survey"]["calibration_grid"]
    grid.update(grid_width_east_mm=200,grid_height_up_mm=140,black_square_side_mm=20,
                square_columns=10,square_rows=7,extent_source="owner_corrected_full_pattern")
    origin=np.asarray(grid["derived_reference_xyz"])*.0254
    for obj in data["features"]:
        if obj.get("kind")=="grid" and obj.get("label")=="North wall calibration grid":
            obj["vertices_m"]=[(origin+[x,0,z]).tolist() for x,z in [(0,0),(.2,0),(.2,.14),(0,.14),(0,0)]]
    data["visual_grid_reference"]=ref
    if "visual_grid_survey_revision" not in data:
        data["survey_revision"]=int(data.get("survey_revision",0))+1
    data["visual_grid_survey_revision"]=data["survey_revision"]
    data["features"]=[f for f in data["features"] if f.get("source")!="retained_visual_grid"]
    for i,row in enumerate(np.asarray(meta["world_corners_m"]).reshape(6,9,3)):
        data["features"].append(dict(kind="grid",label="North wall checkerboard row "+str(i+1),
            vertices_m=row.tolist(),source="retained_visual_grid",landmark_indices=list(range(9*i,9*i+9)),
            scale_status=ref["dimension_status"]))
    atomic_json(path,data)
link=state/"survey-visual-reference"
if link.is_symlink() and link.resolve()!=target.resolve(): raise RuntimeError("different active reference")
if not link.exists(): link.symlink_to(target,target_is_directory=True)
print("persisted",ref["archive_frames_with_matches"],ref["holdout_median_rms_px"],link)
