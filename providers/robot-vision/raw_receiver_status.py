"""Local raw-receiver status, independent of mapping and publication work.

This is an observation, not a health verdict. Camera/ingress status values are
returned unchanged; callers must enforce identity and freshness themselves.
"""
import time


def receiver_status(service):
    started = time.monotonic()
    map_id = getattr(service, "map_id", None)
    camera = getattr(service, "camera", None)
    front = camera.status() if camera is not None else None
    rig = getattr(service, "camera_rig", None)
    rear = None
    if rig is not None:
        camera = rig.camera
        sensors = rig.sensors
        # Do not call rig.status(): it also waits for publisher.status().
        rear = {
            "map_id": map_id,
            "enabled": rig.config["rear"]["enabled"],
            "alive": bool(rig.thread and rig.thread.is_alive()),
            "processed_frames": rig.processed,
            "front_pose_samples": len(rig.front),
            "error": rig.error,
            "camera": camera.status() if camera is not None else None,
            "sensor_push": sensors.status() if sensors is not None else None,
        }
    return {
        "schema": "biome.vision.receiver-status.v1",
        "availability": "available",
        "state": service._lifecycle_state if not service._stop.is_set() else "stopping",
        "map_id": map_id,
        "camera": front,
        "camera_rig": rear,
        "observation_started_monotonic_s": started,
        "observation_finished_monotonic_s": time.monotonic(),
        "tracking_health_claimed": False,
    }
