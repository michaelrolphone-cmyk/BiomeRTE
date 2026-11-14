"""Preserve complete live telemetry without an artificial metadata size cap."""
import copy
import json

def size(value):
    return len(json.dumps(value,separators=(",",":"),allow_nan=False).encode())

def complete_registration(registration, baseline):
    # These keys identify the baseline evaluated, even when fitting is pending.
    for key, source in (("frame_id", "frame_id"), ("baseline_epoch", "epoch"),
                        ("baseline_revision", "revision")):
        registration.setdefault(key, baseline.get(source))
    for key in ("T_anchor_map", "sample_count", "current_camera"):
        registration.setdefault(key, None)
    camera = registration["current_camera"]
    if isinstance(camera, dict):
        for key in ("position_anchor_m", "R_anchor_camera", "age_s", "frame_id"):
            camera.setdefault(key, None)


def complete_tof(tof):
    if not isinstance(tof.get("registration"), dict):
        tof["registration"] = {"valid":False}
    registration = tof["registration"]
    complete_registration(registration, tof.get("baseline") or {})
    refinement = tof.get("rover_refinement")
    if refinement is None:
        tof["rover_refinement"] = {"range_exclusions":None}
    else:
        refinement.setdefault("range_exclusions", [] if "ranges" in refinement else None)


def complete_world_registration(page):
    world = page["map"]
    registration = world["metadata"]["tof_registration"]
    registration.setdefault("map_id", world["map_id"])
    registration.setdefault("map_revision", world["revision"])
    complete_registration(registration, {})
    floor = registration.setdefault("floor", {"valid":False})
    floor.setdefault("valid", False)
    floor.setdefault("T_floor_anchor", None)
    surveyed = registration.setdefault("surveyed_world", {})
    if isinstance(surveyed, dict):
        surveyed.setdefault("axes", None)


def bounded_metadata(metadata, maximum=None):
    """Compatibility entry point; maximum is ignored and all fields are retained."""
    value = copy.deepcopy(metadata)
    if "frame_id" in value:
        if not isinstance(value.get("tof"), dict):
            value["tof"] = {}
        complete_tof(value["tof"])
        if value.get("motion_events") is None:
            value["motion_events"] = {"state":{"available":False}, "events":[]}
        # This serializer preserves all recent events supplied by the detector.
        delivery = value.setdefault("event_delivery", {})
        delivery.setdefault("recent_events_omitted", 0)
    return value
