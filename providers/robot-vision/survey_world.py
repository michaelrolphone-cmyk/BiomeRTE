"""Materialize the owner survey and capture-clock observations in ENU metres."""
import json
import math
from pathlib import Path

import numpy as np


def build_survey(folder):
    folder = Path(folder)
    def read(name):
        return json.loads((folder / name).read_text())
    survey, model = read("survey.json"), read("robot-model.json")
    observations, radio = read("observations.json"), read("ranging.json")
    last = observations[-1]
    facts = []
    inch = .0254
    def feature(kind, label, vertices, **properties):
        facts.append(dict(kind=kind, label=label, vertices_m=np.asarray(vertices, float).tolist(), **properties))
    def wall_points(points):
        return [[x*inch,y*inch,0] for x,y in points]
    def box(name, x, y, w, h):
        points = [[x,y],[x+w,y],[x+w,y+h],[x,y+h],[x,y]]
        feature("furniture", name, wall_points(points), height_known=False)
    room = survey["room"]
    directions = {"east":(1,0),"west":(-1,0),"north":(0,1),"south":(0,-1)}
    x,y = room["perimeter_start_xy"]
    for direction, distance, name, source in room["perimeter_walk"]:
        dx,dy = directions[direction]
        dest = [x+dx*distance, y+dy*distance]
        feature("door" if name == "Door opening" else "wall", name, wall_points([[x,y],dest]), source=source)
        x,y = dest
    corners = room["derived_corners_xy"]
    furniture = survey["furniture"]
    sx,sy = corners["southeast"]; wx,wy = corners["southwest"]; nx,ny = corners["northwest"]
    east,west = furniture["east_alcove_bed"],furniture["west_bed"]
    box("East alcove bed",sx-east["width_west"],sy,east["width_west"],east["length_north"])
    box("West bed",wx,wy,west["width_east"],west["length_north"])
    table = furniture["table"]; box("Table",wx+west["width_east"],wy,table["derived_width_east_west"],table["north_offset_from_wall"])
    dresser = furniture["dresser"]; box("Dresser",nx,ny-dresser["north_end_south_of_northwest_corner"]-dresser["length_south"],dresser["depth_east"],dresser["length_south"])
    radios = survey["fixed_ranging_radios"]
    for name, point in radios["xyz_from_survey"].items():
        feature("radio", name, [np.array(point)*inch], source="survey")
    for route, sequence in radios["measured_ground_routes"].items():
        if route=="leg_format": continue
        start = route.split("_to_")[0]; x,y = radios["xyz_from_survey"][start][:2]; line=[[x,y]]
        for direction,distance in sequence:
            dx,dy=directions[direction];x+=dx*distance;y+=dy*distance;line.append([x,y])
        feature("route",route.replace("_"," "),wall_points(line))
    grid=survey["calibration_grid"];g=np.array(grid["derived_reference_xyz"])*inch
    w,h = grid["grid_width_east_mm"]/1000,grid["grid_height_up_mm"]/1000
    feature("grid","North wall calibration grid",[g,g+[w,0,0],g+[w,0,h],g+[0,0,h],g],square_m=grid["black_square_side_mm"]/1000)
    cp=survey["rover"]["camera_checkpoint"]; yaw=math.radians(cp["heading_degrees"])
    R=np.array([[math.sin(yaw),-math.cos(yaw),0],[math.cos(yaw),math.sin(yaw),0],[0,0,1]])
    cam=np.array(cp["derived_camera_xyz"])*inch
    origin=cam-R@model["camera"]["position_body_m"]
    def body(p): return origin+R@np.asarray(p)
    def pose(rotation,translation):
        out=np.eye(4);out[:3,:3]=rotation;out[:3,3]=translation;return out.tolist()
    body_pose=pose(R,origin); camera_pose=(np.array(body_pose)@model["camera"]["T_robot_camera"]).tolist()
    imu=body(model["ahrs_mounting"]["position_body_m"]);imu_pose=pose(R@model["ahrs_mounting"]["R_body_from_imu"],imu)
    geo=model["geometry"]; a,b=geo["body_length_m"]/2,geo["body_width_m"]/2
    feature("rover","Rover at checkpoint",[body([u,v,0]) for u,v in [(a,b),(a,-b),(-a,-b),(-a,b),(a,b)]])
    feature("heading","Heading reference",[cam,cam+R[:,0]*.22],degrees=cp["heading_degrees"],uncertainty_deg=cp["heading_uncertainty_degrees"])
    feature("camera","Camera optical center",[cam]);feature("ahrs","AHRS",[imu])
    packet=last["frame"]["sensor_packet"]; sonar=[]
    for name,mount in model["sonar_poses"].items():
        xyz=body(mount["position_body_m"]);direction=R@mount["direction_body"]
        feature("sensor",name+" sonar",[xyz],direction=direction.tolist())
        sample=next((s for s in packet["ultrasonic"]["ultrasonic"]["sensors"] if s["id"]==name),{})
        sonar.append(dict(sensor=name,origin_m=xyz.tolist(),direction=direction.tolist(),observation=sample))
        if sample.get("valid") and isinstance(sample.get("distance_m"),(int,float)):
            feature("echo",name+" echo",[xyz,xyz+direction*sample["distance_m"]],distance_m=sample["distance_m"],age_ms=sample.get("age_ms"))
    ah=packet["ahrs"]["ahrs"];att=ah["attitude"]
    ref=dict(T_enu_body=body_pose,T_enu_camera=camera_pose,T_enu_imu=imu_pose,heading_deg=cp["heading_degrees"],heading_uncertainty_deg=cp["heading_uncertainty_degrees"],source="owner_survey")
    if att.get("valid") and att.get("quaternion_wxyz") and att.get("age_ms",999999)<=ah["freshness_ms"]["attitude"]:
        from scipy.spatial.transform import Rotation
        qw,qx,qy,qz=att["quaternion_wxyz"]; native=Rotation.from_quat([qx,qy,qz,qw]).as_matrix()
        ref.update(R_enu_attitude_reference=(np.asarray(imu_pose)[:3,:3]@native.T).tolist(),attitude_generation=att.get("generation"),boot_id=packet["boot_id"])
    ranges=[{k:r.get(k) for k in ("target_bssid","distance_m","samples","direction","age_s","consistent","directional_measurements")} for r in radio.get("rover_ranges",[])]
    samples = {}
    for entry in observations:
        sensor = entry["frame"]["sensor_packet"]["ahrs"]["ahrs"]["imu"]
        if sensor.get("valid") and sensor.get("age_ms", 99999) <= ah["freshness_ms"]["imu"]:
            samples[sensor["observed_uptime_ms"]] = sensor
    if samples:
        imu_rotation = np.asarray(imu_pose)[:3,:3]
        expected = imu_rotation.T @ np.array([0.,0.,9.80665])
        acceleration = np.mean([s["accel_m_s2"] for s in samples.values()],axis=0)
        gyro = np.mean([s["gyro_rad_s"] for s in samples.values()],axis=0)
        ref["imu_checkpoint"] = dict(boot_id=packet["boot_id"], source_frame=last["frame_id"],
            count=len(samples), gyro_residual_imu_rad_s=gyro.tolist(),
            expected_stationary_accel_imu_m_s2=expected.tolist(),
            observed_accel_imu_m_s2=acceleration.tolist(),
            single_pose_accel_residual_m_s2=(acceleration-expected).tolist(),
            absolute_yaw_source="owner_heading", fused_attitude_valid=bool(att.get("valid")),
            north_reference_heading_deg=cp["heading_degrees"])
    positions = {p["bssid"].lower():p for p in (radio.get("baseline") or {}).get("positions", [])}
    for result in ranges:
        p = positions.get(result["target_bssid"].lower())
        if p:
            result["peer_label"] = p.get("label") or result["target_bssid"]
            result["peer_position_source"] = p.get("position_source")
            if p.get("position_source")=="measured_fixed":
                anchor = np.array([p["x_m"],p["y_m"],p["z_m"]])
                result["camera_to_anchor_m"] = float(np.linalg.norm(anchor-cam))
                feature("ftm", result["peer_label"]+" FTM", [cam,anchor],
                    measured_range_m=result["distance_m"],peer=result["target_bssid"])
    return dict(schema="rover.survey.world/1",units="m",axes=["east","north","up"],survey=survey,features=facts,reference=ref,
        checkpoint=dict(frame_id=last["frame_id"],received_at=last["frame"]["receive_decode_time"],image_count=len(list(folder.glob("*.jpg"))),
          snapshot_count=len(observations),valid_visual_poses=sum(bool(s["localization"].get("valid")) for s in observations),
          native_pose=last["localization"],imu=ah["imu"],attitude=att,mag=ah["mag"],barometer=ah["barometer"],
          gyro_calibration=ah["calibration"],sonar=sonar,ranging=ranges,ranging_updated_at=radio["updated_at"],model_id=model["model_id"],model_revision=model["revision"]))
