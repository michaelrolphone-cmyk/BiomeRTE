"""Public identities and versioned envelopes for leased rover telemetry."""
import re
import json
from functools import lru_cache
from pathlib import Path

CAPABILITY = "robot.vision.stream:1"
SCHEMAS = {
    "sensor_manifest": "rover.sensor.manifest/1",
    "world_snapshot": "rover.world.snapshot/1",
    "frame": "rover.vision.frame/1",
    "sensor_tick": "rover.sensor.tick/1",
    "map_delta": "rover.map.delta/1",
    "snapshot_required": "rover.map.snapshot-required/1",
}
NETWORK = re.compile(r"(?i)\b(?:https?|wss?|rtsp)://[^\s\"'<>]+|(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")


def public_data(value):
    """Strip route fields from nested device/atlas records, including old checkpoints."""
    if isinstance(value, dict):
        return {key: public_data(item) for key, item in value.items()
                if key.lower() not in {"ip", "ip_address", "ipv4", "url", "rtsp_url"}
                and not NETWORK.search(key)}
    if isinstance(value, list):
        return [public_data(item) for item in value]
    if isinstance(value, str):
        return NETWORK.sub("[device route]", value)
    return value


def envelope(metadata, resource):
    """Bind private route annotations to the leased public resource at egress."""
    value = public_data(metadata)
    # Older readers consume the same AHRS availability field as robot.ahrs.snapshot.
    ahrs = value.get("ahrs")
    if isinstance(ahrs, dict) and isinstance(ahrs.get("state"), str):
        ahrs.setdefault("availability", ahrs["state"])
    tof = value.get("tof")
    if isinstance(tof, dict) and isinstance(tof.get("camera"), str) and NETWORK.search(metadata["tof"]["camera"]):
        tof["camera"] = resource
    value["schema"] = SCHEMAS[value["type"]]
    value["schema_version"] = dict(_schemas()[value["schema"]]["version"])
    if value["type"] == "sensor_manifest":
        for sensor in value["sensors"]:
            sensor["stream"]["resource"] = resource
    return value

@lru_cache(maxsize=1)
def _schemas():
    return json.loads(Path(__file__).with_name("rover-stream-schemas.json").read_text())


def catalog(schema=None):
    schemas = _schemas()
    if schema is None:
        return {"schemas": schemas}
    if schema not in schemas:
        raise ValueError("unknown_rover_stream_schema")
    return schemas[schema]

def map_snapshot(read, map_id, revision):
    """Resolve the complete local map at the requested revision for every client."""
    if not isinstance(map_id, str) or type(revision) is not int or revision < 0:
        raise ValueError("expected_map_identity_and_revision")
    entities, seen, cursor = [], set(), None
    while True:
        inputs = {"map_id": map_id, "revision": revision, "limit": 500}
        if cursor is not None:
            inputs["cursor"] = cursor
        page = read("robot.map.read", inputs)
        metadata = page["map"]
        if metadata.get("map_id", metadata.get("id")) != map_id or metadata["revision"] != revision:
            raise ValueError("map_snapshot_revision_changed")
        entities.extend(page["entities"])
        cursor = page.get("next_cursor")
        if not cursor:
            break
        import json
        key = json.dumps(cursor, sort_keys=True)
        if key in seen:
            raise ValueError("repeated_map_snapshot_cursor")
        seen.add(key)
    return {"type":"snapshot", "map_id":map_id, "revision":revision, "complete":True,
            "data": public_data({"map":metadata,"entities":entities,"next_cursor":None})}
