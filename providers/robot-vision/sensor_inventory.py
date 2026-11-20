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
        tof=meta.get("tof") or {}
        if tof.get("schema")=="rover.tof.solution/1" and isinstance(tof.get("collector_age_s"),(int,float)):
            add("tof","front","tof",tof.get("updated_at"),received-tof["collector_age_s"])
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
        from sensor_rig_registry import manifest
        with self.lock:
            observations = dict(self.samples)
        return manifest(service, observations)
