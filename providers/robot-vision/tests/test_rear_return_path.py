"""Accepted-socket and real-listener boundaries for the opt-in return-path trial."""
import asyncio
import errno
import json
import socket
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rear_return_path import RearReturnPathCS5
from rear_sensor_push import RearSensorIngress
from test_capture_packet import packet as capture_packet
from test_rear_sensor_push import packet as sensor_packet, rig


def identity(role="camera", **changes):
    value = {"schema": "biome.capture.v1" if role == "camera" else "biome.sensor_tick.v1",
             "receiver_id": "rear-test", "camera_id": "rear"}
    value.update(changes)
    return value


def connection(accepted):
    return SimpleNamespace(transport=SimpleNamespace(get_extra_info=lambda key:
        accepted if key == "socket" else ("127.0.0.1", 9001 if key == "sockname" else 9002)))


class ReturnPathTests(unittest.TestCase):
    def trial(self, **changes):
        args = dict(enabled=True, receiver_id="rear-test", role="camera")
        args.update(changes)
        return RearReturnPathCS5(**args)

    def test_actual_accepted_ipv4_socket_readback_once_and_other_sockets_unchanged(self):
        with socket.socket() as listener, socket.socket() as client:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            client.connect(listener.getsockname())
            accepted, _ = listener.accept()
            with accepted:
                accepted.settimeout(1)
                client.settimeout(1)
                before = accepted.getsockopt(socket.IPPROTO_IP, socket.IP_TOS)
                listener_before = listener.getsockopt(socket.IPPROTO_IP, socket.IP_TOS)
                client_before = client.getsockopt(socket.IPPROTO_IP, socket.IP_TOS)
                trial = self.trial()
                result = trial.after_validated_packet(connection(accepted), identity())
                self.assertEqual(result["status"], "applied")
                self.assertEqual(result["previous_tos"], before)
                self.assertEqual(result["readback_tos"], 0xa0)
                self.assertEqual(listener.getsockopt(socket.IPPROTO_IP, socket.IP_TOS), listener_before)
                self.assertEqual(client.getsockopt(socket.IPPROTO_IP, socket.IP_TOS), client_before)
                # A second packet must not reapply or claim a fresh readback.
                accepted.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, before)
                self.assertIsNone(trial.after_validated_packet(connection(accepted), identity()))
                self.assertEqual(accepted.getsockopt(socket.IPPROTO_IP, socket.IP_TOS), before)
                client.sendall(b"still open")
                self.assertEqual(accepted.recv(32), b"still open")
                accepted.sendall(b"ack")
                self.assertEqual(client.recv(32), b"ack")

    def test_default_off_requires_literal_true_and_never_inspects_socket(self):
        conn = Mock()
        for enabled in (None, False, 0, 1, "true", []):
            with self.subTest(enabled=enabled):
                self.assertIsNone(self.trial(enabled=enabled).after_validated_packet(conn, identity()))
        conn.transport.get_extra_info.assert_not_called()

    def test_front_unmatched_role_and_wrong_schema_never_touch_socket(self):
        conn = Mock()
        for role, packet in (("camera", identity(camera_id="front")),
                             ("camera", identity(receiver_id="other")),
                             ("camera", identity(schema="biome.sensor_tick.v1")),
                             ("viewer", identity()), ("front", identity()),
                             ("sensors", identity()), ("camera", None)):
            with self.subTest(role=role, packet=packet):
                trial = self.trial(role=role)
                self.assertIsNone(trial.after_validated_packet(conn, packet))
                self.assertFalse(trial.attempted)
        conn.transport.get_extra_info.assert_not_called()

    def test_unmatched_identity_does_not_consume_later_valid_attempt(self):
        raw = Mock(family=socket.AF_INET)
        raw.getsockopt.side_effect = [0, 0xa0]
        trial = self.trial()
        self.assertIsNone(trial.after_validated_packet(connection(raw), identity(camera_id="front")))
        self.assertEqual(trial.after_validated_packet(connection(raw), identity())["status"], "applied")
        raw.setsockopt.assert_called_once_with(socket.IPPROTO_IP, socket.IP_TOS, 0xa0)

    def test_ipv6_and_missing_socket_are_explicit_without_setting_anything(self):
        raw = Mock(family=socket.AF_INET6)
        trial = self.trial()
        result = trial.after_validated_packet(connection(raw), identity())
        self.assertEqual(result["status"], "not_ipv4")
        raw.getsockopt.assert_not_called()
        raw.setsockopt.assert_not_called()
        self.assertIsNone(trial.after_validated_packet(connection(raw), identity()))
        result = self.trial().after_validated_packet(connection(None), identity())
        self.assertEqual(result["status"], "socket_unavailable")

    def test_option_failures_are_once_only_sanitized_and_do_not_close(self):
        for stage in ("get_before", "set", "get_after"):
            with self.subTest(stage=stage):
                raw = Mock(family=socket.AF_INET)
                failure = OSError(errno.ENOPROTOOPT, "DO_NOT_RETAIN_PRIVATE_DETAILS")
                raw.getsockopt.side_effect = ([failure] if stage == "get_before" else
                                             [0, failure] if stage == "get_after" else [0])
                if stage == "set":
                    raw.setsockopt.side_effect = failure
                trial = self.trial()
                result = trial.after_validated_packet(connection(raw), identity())
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["error_stage"], stage)
                self.assertEqual(result["error_errno"], errno.ENOPROTOOPT)
                self.assertNotIn("DO_NOT_RETAIN", json.dumps(result))
                calls = list(raw.mock_calls)
                self.assertIsNone(trial.after_validated_packet(connection(raw), identity()))
                self.assertEqual(raw.mock_calls, calls)
                raw.close.assert_not_called()

    def test_unsupported_wrapper_and_mismatched_readback_remain_truthful(self):
        result = self.trial().after_validated_packet(SimpleNamespace(), identity())
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_type"], "AttributeError")
        raw = Mock(family=socket.AF_INET)
        raw.getsockopt.side_effect = [0, 0]
        result = self.trial().after_validated_packet(connection(raw), identity())
        self.assertEqual(result["status"], "readback_mismatch")
        self.assertEqual(result["readback_tos"], 0)


class ListenerReturnPathTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rig, self.output = rig(self.tmp.name)
        self.delivered = []
        self.rig.camera = SimpleNamespace(push_jpeg=lambda *args: self.delivered.append(args))
        self.ingress = RearSensorIngress(self.rig)
        self.ingress.config = {"receiver_id": "rear-test", "listen": "127.0.0.1", "port": 0,
                               "token": "x" * 32, "return_path_cs5_trial": True}
        self.task = asyncio.create_task(self.ingress.serve())
        for _ in range(200):
            if hasattr(self.ingress, "bound_port"):
                break
            if self.task.done():
                await self.task
            await asyncio.sleep(.01)
        self.base = "ws://127.0.0.1:" + str(self.ingress.bound_port)

    async def asyncTearDown(self):
        self.ingress.stop.set()
        await asyncio.wait_for(self.task, 3)
        self.tmp.cleanup()

    def connect(self, path, token=None):
        from websockets.asyncio.client import connect
        return connect(self.base + path, additional_headers={
            "Authorization": "Sensor " + ("x" * 32 if token is None else token)})

    async def receive_type(self, ws, kind):
        async def next_message():
            while True:
                item = json.loads(await ws.recv())
                if item["type"] == kind:
                    return item
        return await asyncio.wait_for(next_message(), 2)

    def evidence(self):
        return self.ingress.status()["return_path_cs5_trial"]

    def camera_wire(self, tick=1000000, **changes):
        value = capture_packet(t=tick)
        value.update(receiver_id="rear-test", camera_id="rear")
        value.update(changes)
        header = json.dumps(value).encode()
        return len(header).to_bytes(4, "big") + header + bytes.fromhex("ffd8ffe0ffd9")

    async def test_real_sensor_validation_then_one_readback_per_connection(self):
        async with self.connect("/sensors") as ws:
            clock = await self.receive_type(ws, "clock")
            self.assertEqual(self.evidence()["attempts"], 0)
            await ws.send(json.dumps({"type": "clock", "id": clock["id"], "boot_id": "boot-a",
                                      "received_monotonic_us": 1000000, "served_monotonic_us": 1000000}))
            for sequence in (1, 2):
                await ws.send(json.dumps(sensor_packet(sequence=sequence, tick=1000000 + sequence)))
                self.assertEqual((await self.receive_type(ws, "ack"))["stream_sequence"], sequence)
            result = self.evidence()
            self.assertEqual(result["attempts"], 1)
            self.assertEqual(result["last"]["sensors"]["status"], "applied")
            self.assertEqual(result["last"]["sensors"]["readback_tos"], 0xa0)
            self.assertEqual(result["last"]["sensors"]["local_port"], self.ingress.bound_port)
            self.assertEqual(result["last"]["sensors"]["peer_port"], ws.local_address[1])
            # Status returns a detached bounded copy, not mutable live state.
            result["last"]["sensors"]["readback_tos"] = -1
            self.assertEqual(self.evidence()["last"]["sensors"]["readback_tos"], 0xa0)
        async with self.connect("/sensors") as ws:
            await self.receive_type(ws, "clock")
            await ws.send(json.dumps(sensor_packet(boot="new-boot")))
            await self.receive_type(ws, "ack")
            self.assertEqual(self.evidence()["attempts"], 2)

    async def test_camera_preview_and_aligned_delivery_share_one_attempt(self):
        async with self.connect("/camera") as ws:
            clock = await self.receive_type(ws, "clock")
            self.assertEqual(self.evidence()["attempts"], 0)
            await ws.send(self.camera_wire())
            await self.receive_type(ws, "camera_ack")
            self.assertEqual(self.evidence()["last"]["camera"]["status"], "applied")
            self.assertEqual(len(self.output), 1)
            await ws.send(json.dumps({"type": "clock", "id": clock["id"], "boot_id": "boot",
                                      "received_monotonic_us": 1000000, "served_monotonic_us": 1000000}))
            await self.receive_type(ws, "camera_ready")
            await ws.send(self.camera_wire(tick=1000010))
            await self.receive_type(ws, "camera_ack")
            self.assertEqual(len(self.delivered), 1)
            self.assertEqual(self.evidence()["attempts"], 1)

    async def test_default_off_preserves_real_sensor_delivery_without_option_attempt(self):
        self.ingress.config.pop("return_path_cs5_trial")
        async with self.connect("/sensors") as ws:
            await self.receive_type(ws, "clock")
            await ws.send(json.dumps(sensor_packet()))
            await self.receive_type(ws, "ack")
        self.assertEqual(self.evidence(), {"enabled": False, "attempts": 0, "last": {}})

    async def test_actual_socket_option_error_does_not_block_sensor_delivery(self):
        # An unsupported option number makes the real accepted socket fail locally;
        # no mocked transport, board connection, or changed network policy is used.
        with patch("rear_return_path.socket.IP_TOS", 1 << 30):
            async with self.connect("/sensors") as ws:
                await self.receive_type(ws, "clock")
                for sequence in (1, 2):
                    await ws.send(json.dumps(sensor_packet(sequence=sequence, tick=1000000 + sequence)))
                    await self.receive_type(ws, "ack")
        result = self.evidence()
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["last"]["sensors"]["status"], "error")
        self.assertEqual(result["last"]["sensors"]["error_stage"], "get_before")

    async def test_wrong_authentication_and_wrong_role_are_not_marked(self):
        from websockets.exceptions import InvalidStatus, ConnectionClosed
        with self.assertRaises(InvalidStatus):
            async with self.connect("/sensors", token="wrong"):
                self.fail("Unauthenticated connection was accepted")
        async with self.connect("/sensors") as ws:
            await self.receive_type(ws, "clock")
            bad = sensor_packet()
            bad["camera_id"] = "front"
            await ws.send(json.dumps(bad))
            with self.assertRaises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(), 2)
        self.assertEqual(self.evidence()["attempts"], 0)

    async def test_malformed_camera_is_rejected_before_trial_callback(self):
        from websockets.exceptions import ConnectionClosed
        for payload in (self.camera_wire()[:-1], self.camera_wire(camera_id="front"),
                        self.camera_wire(receiver_id="unmatched")):
            with self.subTest(payload=payload[:4]), self.assertLogs("websockets.server", level="ERROR"):
                async with self.connect("/camera") as ws:
                    await self.receive_type(ws, "clock")
                    await ws.send(payload)
                    with self.assertRaises(ConnectionClosed):
                        await asyncio.wait_for(ws.recv(), 2)
            self.assertEqual(self.evidence()["attempts"], 0)


if __name__ == "__main__":
    unittest.main()
