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
    if size(value) > maximum:
        raise ValueError("Live metadata exceeds stream header budget: "+str({k:size(v) for k,v in value.items()}))
    return value
