import json
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiohttp import WSMsgType

from tests.qualify_live_provider_recovery import assess, capture
from tests import test_live_provider_recovery_qualification as fixtures


def tick(camera, time_us, **fields):
    return {"type": "sensor_tick", "camera_id": camera, "sensor_packet": {
        "schema": "biome.sensor_tick.v1", "receiver_id": camera + "-device",
        "boot_id": "boot", "capture_monotonic_us": time_us, "frame_id": f"boot:{time_us}",
        "capture_clock": "esp_timer", **fields}}


def lidar(sequence, stamp, zones=64):
    return {"id": "rear", "ready": True, "valid": True, "sequence": sequence,
            "observed_monotonic_us": stamp, "resolution": zones,
            "distance_mm": [370] * zones, "target_status": [5] * zones}


def front(stamp):
    identity = {"boot_id": "boot", "receiver_id": "front-device"}
    return {"ahrs": dict(identity, ahrs={"imu": {
                "valid": True, "present": True, "observed_uptime_ms": stamp // 1000,
                "sample_count": stamp // 20000, "accel_m_s2": [0, -9.8, 0], "gyro_rad_s": [0, 0, 0]},
                "attitude": {"valid": True, "age_ms": 10, "quaternion_wxyz": [1, 0, 0, 0]}}),
            "ultrasonic": dict(identity, ultrasonic={"sensors": [{"id": "left", "status": "ok",
                "valid": True, "sequence": stamp // 20000, "observed_monotonic_us": stamp,
                "distance_m": .56, "echo_us": 3300}]})}


class SensorMeasurementQualification(unittest.IsolatedAsyncioTestCase):
    async def observe(self, *records):
        messages = []
        for row in records:
            header = json.dumps(row).encode()
            messages.append(SimpleNamespace(type=WSMsgType.BINARY, data=struct.pack("!I", len(header)) + header))
        socket = MagicMock()
        socket.receive = AsyncMock(side_effect=[*messages, SimpleNamespace(type=WSMsgType.CLOSE)])
        session = MagicMock()
        session.ws_connect.return_value.__aenter__.return_value = socket
        return (await capture(session, "ws://localhost", "fixture", 2))["feeds"]

    async def test_real_sample_clocks_advance_when_stationary_and_replays_do_not(self):
        rows = await self.observe(tick("front", 1000000, **front(990000)),
                                  tick("front", 1100000, **front(1090000)),
                                  tick("front", 1200000, **front(1090000)),
                                  tick("front", 1300000, **front(990000)))
        self.assertEqual(rows["sensor_tick:front"]["instrument_samples"],
                         {"imu.front": 2, "ahrs.front": 2, "ultrasonic.left": 2})

    async def test_expected_lidar_must_deliver_valid_grids_with_new_acquisition_clocks(self):
        manifest = {"type": "sensor_manifest", "sensors": [
            {"id": "lidar.front", "type": "lidar", "status": "offline"},
            {"id": "lidar.rear", "type": "lidar", "status": "online"}]}
        def packet(t, row):
            return tick("rear", t, lidar={"sensors": [row]})
        rows = await self.observe(manifest, packet(1000000, lidar(8, 990000)),
                                  packet(1100000, lidar(9, 1090000)),
                                  packet(1200000, lidar(10, 1090000)),
                                  packet(1300000, dict(lidar(11, 1290000), target_status=[])),
                                  packet(1400000, lidar(12, 1401000)))
        self.assertEqual(rows["sensor_tick:rear"]["instrument_samples"], {"lidar.rear": 2})
        empty = await self.observe(manifest, tick("rear", 1000000), tick("rear", 1100000))
        self.assertEqual(empty["sensor_tick:rear"]["instrument_samples"], {"lidar.rear": 0})

    def test_tick_counts_alone_cannot_certify_any_consumer(self):
        fixture = fixtures.LiveProviderRecoveryQualification()
        fixture.setUp()
        sample = fixture.evidence["streams"]["public"]["feeds"]["sensor_tick:front"]
        for envelope in ({}, {"imu.front": 1}, {"imu.front": 80, "ultrasonic.left": 0}):
            sample["instrument_samples"] = envelope
            self.assertTrue(any("measurement" in message for message in assess(fixture.evidence, 80, 1, 1)))
        sample["instrument_samples"] = {"imu.front": 30}
        sample["instrument_max_gap_seconds"] = {"imu.front": 25}
        self.assertIn("public sensor_tick:front imu.front measurement gap exceeded 1s (25s)",
                      assess(fixture.evidence, 80, 1, 1))

    async def test_wrong_device_rebooted_clock_and_missing_inner_values_do_not_advance(self):
        first = tick("front", 2000000, **front(1990000))
        other = tick("front", 3000000, **front(2990000))
        other["sensor_packet"]["ahrs"]["receiver_id"] = "another-device"
        other["sensor_packet"]["ultrasonic"]["ultrasonic"]["sensors"][0]["distance_m"] = None
        rebooted = tick("front", 1000000, **front(990000))
        rebooted["sensor_packet"].update(boot_id="reboot", frame_id="reboot:1000000")
        for name in ("ahrs", "ultrasonic"):
            rebooted["sensor_packet"][name]["boot_id"] = "reboot"
        rows = await self.observe(first, other, rebooted)
        self.assertEqual(rows["sensor_tick:front"]["instrument_samples"],
                         {"imu.front": 1, "ahrs.front": 1, "ultrasonic.left": 1})
