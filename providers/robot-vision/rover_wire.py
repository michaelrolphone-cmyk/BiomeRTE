"""Public identities and versioned envelopes for leased rover telemetry."""
import re

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
    tof = value.get("tof")
    if isinstance(tof, dict) and isinstance(tof.get("camera"), str) and NETWORK.search(metadata["tof"]["camera"]):
        tof["camera"] = resource
    value["schema"] = SCHEMAS[value["type"]]
    value["schema_version"] = {"major": 1, "minor": 0}
    if value["type"] == "sensor_manifest":
        for sensor in value["sensors"]:
            sensor["stream"]["resource"] = resource
    return value

def catalog(schema=None):
    import json
    from pathlib import Path
    schemas = json.loads(Path(__file__).with_name("rover-stream-schemas.json").read_text())
    if schema is None:
        return {"schemas": schemas}
    if schema not in schemas:
        raise ValueError("unknown_rover_stream_schema")
    return schemas[schema]
