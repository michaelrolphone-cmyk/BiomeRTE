"""Inventory of configured instruments and observations from the shared vision feed."""
import copy
import hashlib
import json
import math
import threading
import time
from datetime import datetime, timezone

from rover_wire import CAPABILITY, SCHEMAS


class SensorInventory:
    def __init__(self):
        self.lock = threading.Lock()
        self.samples = {}

    def observe(self, meta, image):
        packet = meta.get("sensor_packet") or {}
        camera = meta.get("camera_id", "front")
        received = meta.get("processed_monotonic", time.monotonic())
        packet_id = (packet.get("boot_id"), packet.get("stream_sequence"))
        samples = []

        def add(kind, role, channel_path, identity, when, *, status="online",
                rate=None, sensor_id=None, mount=None):
            key = sensor_id or kind + "." + role
            row = {"id": key, "type": kind, "role": role,
                   "schema": SCHEMAS["frame" if kind == "camera" else "sensor_tick"],
                   "rate_hz": rate, "mount": {"T_body_sensor": mount},
                   "stream": {"capability": CAPABILITY, "channel": "vision", "path": channel_path},
                   "status": status}
            if identity is not None and math.isfinite(when):
                samples.append((key, identity, when, row))

        if image:
            add("camera", camera, "payload", meta.get("frame_id"), received)
        raw_ahrs = (packet.get("ahrs") or {}).get("ahrs") or {}
        for name in ("imu", "attitude", "mag", "barometer"):
            sample = raw_ahrs.get(name)
            if isinstance(sample, dict) and sample.get("present") is not False:
                kind = "ahrs" if name == "attitude" else name
                age = sample.get("age_ms") or 0
                add(kind, camera, "sensor_packet.ahrs.ahrs." + name,
                    (packet_id[0], sample.get("observed_uptime_ms", packet_id[1])),
                    received - age / 1000, rate=raw_ahrs.get("sample_rate_hz"),
                    status="error" if sample.get("valid") is False else "online")
        sonar = (packet.get("ultrasonic") or {}).get("ultrasonic") or {}
        for sample in sonar.get("sensors") or []:
            role = sample.get("id") or sample.get("sensor") or sample.get("position")
            if isinstance(role, str):
                add("ultrasonic", role, "sensor_packet.ultrasonic.ultrasonic.sensors",
                    (packet_id[0], sample.get("sequence", packet_id[1])),
                    received - (sample.get("age_ms") or 0) / 1000)
        if packet.get("ftm"):
            add("tof", camera, "sensor_packet.ftm",
                (packet_id[0], packet["ftm"].get("report_id", packet_id[1])), received)
        lidar = (meta.get("lidar") or {}).get("latest") or {}
        for item in lidar.get("sensors") or []:
            sample = item.get("raw") or {}
            role = item.get("sensor_id")
            if isinstance(role, str):
                add("lidar", role, "lidar.latest.sensors",
                    (lidar.get("boot_id"), sample.get("observed_monotonic_us"), sample.get("sequence")),
                    lidar.get("received_monotonic", received) - (sample.get("age_ms") or 0) / 1000,
                    status="online" if sample.get("ready") else "error")
        # Self-describing producers can introduce sensor kinds without a consumer change.
        for sample in packet.get("sensors") or []:
            if isinstance(sample, dict) and all(isinstance(sample.get(k), str) for k in ("id", "type", "role")):
                add(sample["type"], sample["role"], "sensor_packet.sensors",
                    packet_id, received, sensor_id=sample["id"], rate=sample.get("rate_hz"),
                    mount=(sample.get("mount") or {}).get("T_body_sensor"))
        with self.lock:
            for key, identity, when, row in samples:
                previous = self.samples.get(key)
                if previous is None or (identity != previous[0] and when >= previous[1]):
                    self.samples[key] = (identity, when, row)

    def snapshot(self, service):
        definition = service._robot_model().get("definition", {})
        cameras = definition.get("cameras", {})
        rig = service.camera_rig.config["rear"]
        configured = {"camera.front": ("camera", "front", "payload")}
        if rig["enabled"]:
            configured["camera.rear"] = ("camera", "rear", "payload")
        with self.lock:
            observations = dict(self.samples)
        for key in observations:
            row = observations[key][2]
            configured[key] = (row["type"], row["role"], row["stream"]["path"])
        sensors = []
        now = time.monotonic()
        epoch = time.time() - now
        for key, (kind, role, path) in sorted(configured.items()):
            sample = observations.get(key)
            row = copy.deepcopy(sample[2]) if sample else {
                "id": key, "type": kind, "role": role, "schema": SCHEMAS["frame"],
                "stream": {"capability": CAPABILITY, "channel": "vision", "path": path},
                "rate_hz": None, "mount": {"T_body_sensor": None}, "status": "offline"}
            when = sample[1] if sample else None
            row["last_seen"] = (datetime.fromtimestamp(epoch + when, timezone.utc).isoformat(timespec="milliseconds")
                                if when is not None else None)
            if kind == "camera":
                camera_config = rig["camera"] if role == "rear" else service.config["camera"]
                row["rate_hz"] = camera_config.get("expected_fps")
                row["mount"]["T_body_sensor"] = cameras.get(role, {}).get("T_robot_camera")
            elif kind == "lidar":
                row["mount"]["T_body_sensor"] = definition.get("lidar_poses", {}).get(role, {}).get("T_robot_sensor")
            elif kind in ("imu", "ahrs", "mag", "barometer"):
                imu = definition.get("ahrs_mounting", {})
                rotation, position = imu.get("R_body_from_imu"), imu.get("position_body_m")
                if rotation and position:
                    row["mount"]["T_body_sensor"] = [list(axis) + [offset] for axis, offset in zip(rotation, position)] + [[0, 0, 0, 1]]
            elif kind == "ultrasonic":
                pose = definition.get("sonar_poses", {}).get(role, {})
                heading, pos = pose.get("direction_body"), pose.get("position_body_m")
                if heading and pos and heading[2] == 0:
                    x, y, _ = heading
                    row["mount"]["T_body_sensor"] = [[x,-y,0,pos[0]],[y,x,0,pos[1]],[0,0,1,pos[2]],[0,0,0,1]]
            if when is None or now - when > max(2.0, 3 / (row["rate_hz"] or 1)):
                row["status"] = "offline"
            sensors.append(row)
        # Heartbeats advance last_seen. Inventory revision covers topology, mounts and health.
        identity = [{k:v for k,v in s.items() if k != "last_seen"} for s in sensors]
        revision = hashlib.sha256(json.dumps(identity, sort_keys=True, allow_nan=False).encode()).hexdigest()
        return {"type":"sensor_manifest", "schema":SCHEMAS["sensor_manifest"], "revision":revision, "sensors":sensors}
