"""Fit live sensor metadata to the existing 16 KiB stream header contract.

Keep physical measurements and their original clocks. Discard duplicated cache
envelopes and diagnostics before trimming radio scan rows. Report omissions;
the complete cache remains available through the discrete telemetry capability.
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
    if size(value) <= maximum or not any(key in value for key in ("ahrs", "radio", "ultrasonic")):
        return value
    ahrs = value.get("ahrs")
    radio = value.get("radio")
    for sensor in (ahrs, value.get("ultrasonic")):
        if isinstance(sensor, dict):
            snapshot = sensor.pop("snapshot", None)
            if isinstance(snapshot, dict):
                sensor["cache_timing"] = {key: snapshot[key] for key in (
                    "received_at", "received_monotonic", "request_started_monotonic",
                    "request_duration_s", "elapsed_since_receive_s", "age_clock", "timing", "observations")
                    if key in snapshot}
    if isinstance(radio, dict):
        cache = radio.get("telemetry")
        if isinstance(cache, dict):
            firmware = cache.get("telemetry")
            if isinstance(firmware, dict):
                # The RF endpoint repeats the AHRS envelope already carried above.
                firmware.pop("ahrs", None)
                if "ultrasonic" in value:
                    firmware.pop("ultrasonic", None)
            if size(value) > maximum:
                # Retain RF age/index bindings but omit repeated long source IDs.
                refs = cache.get("observations")
                if isinstance(refs, list):
                    cache["source_observation_count"] = len(refs)
                    cache["observations"] = [{k: row[k] for k in
                        ("radio", "index", "effective_age_ms") if k in row}
                        for row in refs if isinstance(row, dict)]
            if size(value) > maximum and isinstance(firmware, dict):
                firmware.pop("config", None)
    # Health diagnostics duplicate fields available through status capabilities.
    if size(value) > maximum and isinstance(ahrs, dict):
        source = ahrs.pop("source", None)
        if isinstance(source, dict):
            ahrs["source_health"] = {k:source[k] for k in ("state", "error", "enabled") if k in source}
    if isinstance(radio, dict):
        for key in ("model", "learning", "localization"):
            if size(value) <= maximum:
                break
            radio.pop(key, None)
        cache = radio.get("telemetry")
        firmware = cache.get("telemetry") if isinstance(cache, dict) else None
        if isinstance(firmware, dict):
            def scans(node):
                if isinstance(node, dict):
                    for item in node.values():
                        yield from scans(item)
                elif isinstance(node, list) and node and isinstance(node[0], dict):
                    yield node
            groups = {kind: list(scans(firmware.get(kind))) for kind in ("wifi", "ble")}
            arrays = groups["wifi"] + groups["ble"]
            omitted = 0
            while size(value) > maximum and any(arrays):
                candidates = [rows for rows in arrays if rows]
                largest = max(candidates, key=lambda rows: size(rows))
                average = max(1, size(largest) // len(largest))
                excess = size(value) - maximum
                count = min(len(largest), max(1, (excess + average - 1) // average))
                del largest[-count:]
                omitted += count
                cache["stream_scan_rows_omitted"] = omitted
            if omitted:
                # Never leave an index pointing beyond a trimmed scan array.
                refs = cache.get("observations", [])
                limits = {kind: max((len(rows) for rows in groups[kind]), default=0)
                          for kind in groups}
                kept = [row for row in refs if row.get("radio") in limits
                        and isinstance(row.get("index"), int)
                        and row["index"] < limits[row["radio"]]]
                cache["observations"] = kept
                cache["stream_reference_rows_omitted"] = len(refs)-len(kept)
    events=value.get("events")
    while size(value)>maximum and isinstance(events,list) and len(events)>1:
        events.pop(0)
        delivery=value.setdefault("event_delivery",{"recent_events_omitted":0,"recovery_capability":"robot.vision.status"})
        delivery["recent_events_omitted"]+=1
    if isinstance(motion,dict) and isinstance(events,list):motion["event_ids"]=[e.get("event_id") for e in events]
    if size(value) > maximum:
        raise ValueError("Live metadata exceeds stream header budget: "+str({k:size(v) for k,v in value.items()}))
    return value
