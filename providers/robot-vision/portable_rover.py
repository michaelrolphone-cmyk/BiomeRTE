"""Portable rover geometry and cache-only metric sonar queries.

Body axes x forward, y left, z up. The floor origin is body-envelope centre.
Physical dimensions and measured mounts are independent of monocular map scale.
"""
import copy
import json
import math
from pathlib import Path
from datetime import datetime, timezone

PORTABLE_OPERATIONS = ["robot.body.model", "robot.control.model",
                       "robot.sonar.snapshot", "robot.metric.status"]
MOTION = {"1": "forward", "2": "back", "3": "left", "4": "right",
          "5": "unassigned", "6": "unassigned"}
DRIVE = {"actuation": "binary_on_off_relays", "pwm_available": False,
         "speed_model": {"independent_variable": "continuous_channel_on_duration_s",
                        "relationship": "speed_follows_duration_dependent_curve",
                        "calibrated": False, "curve": None,
                        "qualification": "not_yet_calibrated"},
         "channel_motion": MOTION, "exclusive_pairs": [[1, 2], [3, 4]],
         "steering_axle": "rear", "steering_requires_rolling": True,
         "steering_and_throttle_may_be_held_together": True,
         "contracts": ["relay.bank:1", "device.outputs.batch:1", "device.outputs.stream:1"]}
EXPOSURE_TIME = {"capture_clock": "ESP32 esp_timer, scoped by boot_id",
    "capture_event": "camera_driver_first_DMA_buffer",
    "exposure_start_time_known": False, "exposure_end_time_known": False,
    "exposure_midpoint_time_known": False, "rolling_shutter_row_timing_known": False,
    "physical_exposure_time_uncertainty_s": None,
    "qualification": "DMA capture timestamp is not calibrated photon exposure time",
    "host_receive_decode_time": "Mac UTC after receipt/decode, not exposure time"}

def body_definition(previous=None, mounts=None):
    model = copy.deepcopy(previous or {})
    model.update(schema_version=1, model_id="rc-car-rear-steering-20261109",
                 revision=max(3, model.get("revision", 0)+1),
                 updated_at=datetime.now(timezone.utc).isoformat())
    model["coordinate_frame"] = {"name": "robot_body", "axes": ["forward","left","up"],
        "origin": "body_envelope_center_projected_on_floor", "units": "m"}
    model["floor_contact"] = {"always_on_floor": True, "source": "owner_operating_invariant",
        "plane": "locally_planar_floor", "caller_confirmation_required": False,
        "pixel_interpretation": "ray intersection with floor, not proof pixel belongs to floor"}
    model["geometry"] = {"body_length_m": .18415, "body_width_m": .1397,
        "body_height_m": .0635, "overall_height_with_vertical_module_m": .127,
        "wheelbase_m": .127, "wheel_diameter_m": .05715, "wheel_width_m": .0254,
        "track_width_m": .1143, "track_width_qualification": "derived_outer_width_minus_tire_width",
        "reference_origin": "body_envelope_center_projected_on_floor",
        "provenance": "owner_dimensions_inches_converted_at_0.0254_m_per_inch"}
    T = [[0,0,1,.047625],[-1,0,0,0],[0,-1,0,.0635],[0,0,0,1]]
    model.setdefault("camera",{}).update(T_robot_camera=T, position_body_m=[.047625,0,.0635],
        setback_from_front_m=.04445, optical_axis_body=[1,0,0],
        axes="camera_optical_x_right_y_down_z_forward",
        mounting_qualification="owner_reported_dimensions",
        exposure_timestamps=EXPOSURE_TIME,
        driving_light={"contract":"robot.camera.light.set:1","purpose":"illuminate_camera_view_for_driving"})
    model.setdefault("ahrs_mounting",{}).update(attachment="glued_in_place", rigid=True,
        position_reference="camera_optical_center", position_offset_camera_m=[0,0,-.004],
        position_body_m=[.043625,0,.0635],
        position_offset_description="4_mm_directly_behind_camera",
        R_camera_imu=[[-1,0,0],[0,1,0],[0,0,-1]],
        orientation_status="owner_axis_checks",
        axis_checks={"left_turn":"positive_yaw","front_up":"negative_roll","right_side_up":"positive_pitch"})
    model.setdefault("control",{}).update(copy.deepcopy(DRIVE))
    model["relay_mapping"] = {key: {"function": value, "provenance":"deployed_dashboard_motion_mapping"}
                              for key,value in MOTION.items()}
    sonar = {}
    for name, mount in (mounts or {}).items():
        o=mount["origin_camera_m"]; d=mount["direction_camera"]
        sonar[name]={**mount, "position_body_m":[.047625+o[2],-o[0],.0635-o[1]],
                     "direction_body":[d[2],-d[0],-d[1]]}
    model["sonar_poses"] = sonar
    return model

def metric_status(service, snapshot=None):
    map_id = (snapshot or {}).get("map_id", service.map_id)
    revision = (snapshot or {}).get("map_revision")
    scale=service.metric.attachment_status(map_id, revision=revision)
    model=service._robot_model()
    definition=model.get("definition", {})
    return {"availability":"available", "units":"m",
        "metric_scale_available": scale.get("availability")=="available",
        "map_scale":scale,
        "local_metric_geometry_available":model.get("availability")=="available",
        "sonar_range_units":"m", "sonar_requires_monocular_scale":False,
        "body_model_revision":definition.get("revision"),
        "checkerboard_reference":definition.get("metric_reference"),
        "camera_height_m":definition.get("camera",{}).get("reported_optical_height_m"),
        "scope_rule":"body_and_sonar_metres_do_not_establish_native_map_m_per_unit"}

def sonar_snapshot(service):
    source=getattr(service,"ultrasonic",None)
    if source is None:
        return {"availability":"unavailable","reason":"sonar_source_not_configured"}
    cached=source.snapshot()
    status=source.status()
    interval=source.poll_interval_s
    base={"units":"m", "coordinate_frame":"robot_body", "status":status,
        "update_rate":{"host_poll_interval_s":interval,"host_poll_hz":1/interval,
                       "sensor_sample_rate_hz":None,
                       "sensor_sample_rate_qualification":"see_firmware_update_interval_ms",
                       "readings_are_sequential_not_simultaneous":True},
        "assessed_at":datetime.now(timezone.utc).isoformat(),
        "age_semantics":"firmware_age_plus_request_duration_plus_host_monotonic_elapsed",
        "body_model_contract":"robot.body.model:1"}
    if cached is None:
        return {**base,"availability":"unavailable","reason":"no_sonar_snapshot",
                "sensors":[{"id":n,"range_m":None,"valid":False,"age_s":None,"fresh":False}
                           for n in ("front","rear","left","right")]}
    raw=cached["telemetry"]["ultrasonic"]
    ages={o["id"]:o.get("effective_age_ms") for o in cached.get("observations",[])}
    model=service._robot_model().get("definition",{})
    sensors=[]
    for row in raw["sensors"]:
        age=ages.get(row["id"])
        valid=row.get("valid") is True
        fresh=bool(cached.get("fresh") and age is not None and age<=1000 and valid)
        sensors.append({"id":row["id"],"range_m":row.get("distance_m") if valid else None,
            "valid":valid,"fresh":fresh,"age_s":None if age is None else age/1000,
            "status":row.get("status"),"sequence":row.get("sequence"),
            "observed_uptime_ms":row.get("observed_uptime_ms"),
            "pose":model.get("sonar_poses",{}).get(row["id"])})
    base["update_rate"]["firmware_update_interval_ms"]=raw.get("update_interval_ms")
    fusion=getattr(service,"fusion",None)
    return {**base,"availability":"available","sensors":sensors,
        "boot_id":cached["telemetry"].get("boot_id"),
        "source_received_at":cached.get("received_at"),
        "source_sequence":cached["telemetry"].get("sequence"),
        "fusion_status":"metric_ranges_with_rigid_body_sensor_poses",
        "all_fresh":all(s["fresh"] for s in sensors)}

def enrich(service, operation, result):
    if not isinstance(result,dict):return result
    if operation in ("robot.vision.metadata","robot.vision.status"):
        result["body_model_capability"]="robot.body.model:1"
        result["drive_model"]=copy.deepcopy(DRIVE)
        result["metric"]=metric_status(service)
        result["metric_scale_available"]=result["metric"]["metric_scale_available"]
        result["local_metric_geometry_available"]=True
        result["camera_exposure_timing"]=copy.deepcopy(EXPOSURE_TIME)
        result["camera_driving_light"]={"contract":"robot.camera.light.set:1",
            "purpose":"illuminate_camera_view_for_driving"}
    snapshot=result.get("snapshot")
    if operation in ("robot.vision.snapshot","robot.vision.geometry"):
        binding=snapshot if isinstance(snapshot,dict) else result
        metric=metric_status(service,binding)
        binding["metric_scale"]=metric["map_scale"]
        binding["metric_scale_available"]=metric["metric_scale_available"]
        binding["local_metric_geometry_available"]=True
        binding["body_model_capability"]="robot.body.model:1"
        if isinstance(binding.get("frame"),dict):
            binding["frame"]["exposure_timestamp_qualification"]=copy.deepcopy(EXPOSURE_TIME)
        evidence=binding.get("spatial_evidence",{})
        if isinstance(evidence.get("ahrs"),dict):
            evidence["ahrs"]["metric_scale_available"]=metric["metric_scale_available"]
        if isinstance(evidence.get("ground_projection"),dict):
            evidence["ground_projection"].update(availability="available_on_floor_plane_query",
                caller_floor_contact_confirmation_required=False)
    return result

def dispatch(service, operation, inputs, fallback):
    if operation=="robot.body.model":return service._robot_model()
    if operation=="robot.control.model":return copy.deepcopy(DRIVE)
    if operation=="robot.sonar.snapshot":return sonar_snapshot(service)
    if operation=="robot.metric.status":return metric_status(service)
    return enrich(service,operation,fallback(operation,inputs))
