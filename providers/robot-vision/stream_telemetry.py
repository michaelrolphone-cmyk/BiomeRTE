"""Fit live sensor metadata to the existing 16 KiB stream header contract.

Keep physical measurements and their original clocks. Discard duplicated cache
envelopes and diagnostics while preserving directional FTM measurements. Report omissions;
only duplicated diagnostics require the discrete telemetry capability.
"""
import copy
import json


def size(value):
    return len(json.dumps(value, separators=(",", ":"), allow_nan=False).encode())


def bounded_metadata(metadata, maximum=15872):
    value = copy.deepcopy(metadata)
    # Dense history is new in this stream. A columnar representation preserves
    # every sample value without repeating sixteen object keys 32 times.
    ahrs_state=(value.get("ahrs") or {}).get("ahrs")
    if isinstance(ahrs_state,dict) and isinstance(ahrs_state.get("imu_samples"),list):
        rows=ahrs_state.pop("imu_samples")
        ahrs_state["imu_history"]={
            "schema":"biome.imu_history.v1",
            "columns":["monotonic_us","sample_count","generation","valid","attitude_valid",
                "ax_m_s2","ay_m_s2","az_m_s2","gx_rad_s","gy_rad_s","gz_rad_s",
                "qw","qx","qy","qz","yaw_deg"],
            "rows":[[r.get("monotonic_us"),r.get("sample_count"),r.get("generation"),
                r.get("valid"),r.get("attitude_valid"),*(r.get("accel_m_s2") or [None]*3),
                *(r.get("gyro_rad_s") or [None]*3),*(r.get("quaternion_wxyz") or [None]*4),
                r.get("yaw_deg")] for r in rows if isinstance(r,dict)]}
    motion=value.get("motion_events")
    if isinstance(motion,dict):
        # One full event representation per packet. The 64-event recovery ring
        # remains available through robot.vision.status.
        events=motion.pop("events",[])
        motion["event_ids"]=[e.get("event_id") for e in events[-8:]]
        motion["events_reference"]="top_level.events"
        motion["history_capability"]="robot.vision.status"
    events=value.get("events")
    if isinstance(events,list) and len(events)>8:
        value["event_delivery"]={"recent_events_omitted":len(events)-8,
            "recovery_capability":"robot.vision.status"}
        value["events"]=events[-8:]
    if size(value) <= maximum or not any(key in value for key in ("ahrs", "tof", "ultrasonic")):
        return value
    ahrs = value.get("ahrs")
    tof = value.get("tof")
    for sensor in (ahrs, value.get("ultrasonic")):
        if isinstance(sensor, dict):
            snapshot = sensor.pop("snapshot", None)
            if isinstance(snapshot, dict):
                sensor["cache_timing"] = {key: snapshot[key] for key in (
                    "received_at", "received_monotonic", "request_started_monotonic",
                    "request_duration_s", "elapsed_since_receive_s", "age_clock", "timing", "observations")
                    if key in snapshot}
    if isinstance(tof,dict) and size(value)>maximum:
        anchors=tof.get("anchors")
        if isinstance(anchors,list):
            compact=[]
            for anchor in anchors:
                if not isinstance(anchor,dict):
                    continue
                item={key:anchor[key] for key in (
                    "id","bssid","boot_id","connected","received_age_s",
                    "ftm_state","channel","tx_power_dbm","height_known","height_m")
                    if key in anchor}
                ranges=anchor.get("ranges")
                if isinstance(ranges,list):
                    item["ranges"]=[{key:report[key] for key in (
                        "target_bssid","status","range_seq","age_ms","rtt_ns",
                        "samples","valid","distance_m") if key in report}
                        for report in ranges if isinstance(report,dict)]
                compact.append(item)
            tof["anchors"]=compact
            tof["anchor_range_details_compacted"]=True
            tof.pop("anchor_range_details_omitted",None)
            tof["detail_capability"]="robot.tof.status"
    if isinstance(tof,dict) and size(value)>maximum and isinstance(tof.get("baseline"),dict):
        baseline=tof["baseline"]
        # Coordinates are already at anchor_geometry. Never duplicate the fit,
        # and keep raw directional measurements for the live matrix.
        baseline.pop("positions",None)
        baseline["positions_reference"]="anchor_geometry"
        # Per-link calibration diagnostics are available through the full capability.
        tof.pop("edges",None)
        tof["baseline_diagnostics_compacted"]=True
        tof["detail_capability"]="robot.tof.status"
    # Health diagnostics duplicate fields available through status capabilities.
    if size(value) > maximum and isinstance(ahrs, dict):
        source = ahrs.pop("source", None)
        if isinstance(source, dict):
            ahrs["source_health"] = {k:source[k] for k in ("state", "error", "enabled") if k in source}
    events=value.get("events")
    while size(value)>maximum and isinstance(events,list) and len(events)>1:
        events.pop(0)
        delivery=value.setdefault("event_delivery",{"recent_events_omitted":0,"recovery_capability":"robot.vision.status"})
        delivery["recent_events_omitted"]+=1
    if isinstance(motion,dict) and isinstance(events,list):motion["event_ids"]=[e.get("event_id") for e in events]
    if size(value)>maximum and isinstance(tof,dict) and isinstance(tof.get("baseline"),dict):
        baseline=tof["baseline"]
        refinement=baseline.get("refinement")
        if isinstance(refinement,dict):
            baseline["refinement"]={k:refinement[k] for k in (
                "revision","accepted","state","last_update_at","before_rms_m","after_rms_m")
                if k in refinement}
            baseline["refinement_details_compacted"]=True
        # Scheduler diagnostics are available through the full status capability.
        if size(value)>maximum:
            tof.pop("request_age_s",None)
            tof["scheduler_diagnostics_omitted"]=True
    if size(value)>maximum:
        # These are status-capability diagnostics, not sensor observations.
        # Preserve every physical sample, original clock and range report.
        for sensor, keys in (
            (ahrs, ("map_id","calibration_id","session_id","accepted_observations",
                "accepted_sensor_endpoints","pending_observation","max_evidence_gap_s",
                "position_verified","navigation_usable","heading_available",
                "camera_pose_corrected","metric_scale_available","motion_commands",
                "physical_capture_time_uncertainty_s")),
            (value.get("ultrasonic"), ("polls","snapshots_accepted","duplicate_snapshots",
                "replayed_snapshots","boot_changes","last_attempt_at","buffer"))):
            if isinstance(sensor,dict):
                for key in keys:sensor.pop(key,None)
        if isinstance(tof,dict) and isinstance(tof.get("baseline"),dict):
            baseline=tof["baseline"]
            for key in ("last_event","last_rejection","load_error","movement_threshold_m",
                        "movement_hold_s","sample_count","edge_count","condition_number"):
                baseline.pop(key,None)
        value["status_diagnostics_compacted"]=True
        value["status_detail_capability"]="robot.vision.status"
    if size(value) > maximum and isinstance(tof,dict):
        registration = tof.get("registration")
        if isinstance(registration,dict):
            # Detailed fit diagnostics remain available through robot.tof.status.
            keep = ("valid","status","map_id","map_revision","frame_id","baseline_epoch",
                    "baseline_revision","T_anchor_map","scale_m_per_map_unit","current_camera",
                    "control_keyframe_ids","fitting","sample_count","validation_rms_m")
            compact = {k:registration[k] for k in keep if k in registration}
            floor = registration.get("floor")
            if isinstance(floor,dict):
                compact["floor"] = {k:floor[k] for k in ("valid","status","T_floor_anchor","normal_anchor") if k in floor}
            compact["detail_capability"] = "robot.tof.status"
            compact["diagnostics_omitted"] = True
            compact["navigation_authority"] = False
            tof["registration"] = compact
        if size(value)>maximum:
            for anchor in tof.get("anchor_geometry",[]):
                prior=anchor.get("floor_prior")
                if isinstance(prior,dict):
                    anchor["floor_prior"]={k:prior[k] for k in ("floor","level") if k in prior}
            for key in ("frame","uncertainty","height_mode","rover_height_m","geometry_quality",
                        "motion_compensated","moving_rover_requires_motion_compensation"):
                tof.pop(key,None)
    if size(value)>maximum and isinstance(tof,dict) and isinstance(tof.get("registration"),dict):
        registration=tof["registration"]
        registration.pop("T_anchor_map",None)
        if isinstance(registration.get("floor"),dict):
            registration["floor"].pop("T_floor_anchor",None)
        registration["transforms_reference"]="robot.map.read:1.metadata.tof_registration"
        registration["world_transform_reference"]="robot.world.read:1.metadata.tof_registration"
    if size(value) > maximum:
        raise ValueError("Live metadata exceeds stream header budget: "+str({k:size(v) for k,v in value.items()}))
    return value
