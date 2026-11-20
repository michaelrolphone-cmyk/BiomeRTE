"""Service and supervisor binding is checked before any helper state access."""
import asyncio
import json
import types
import unittest
from unittest.mock import AsyncMock, Mock

from biomerte.service_checkpoint import PROTOCOL
from biomerte.service_daemon import CheckpointHelper
from biomerte.store import Fault


BINDING = dict(service_id="controller", manifest_revision="revision",
    worker_id="mac", worker_incarnation="boot1", boot_epoch="epoch", grant_token="grant")


class HelperScopeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.helper = CheckpointHelper.__new__(CheckpointHelper)
        self.helper.config = dict(service_id="controller", worker_id="mac", request_timeout_s=.1)
        self.helper.token = "isolated-helper-secret"
        self.helper.transport = types.SimpleNamespace(
            request=AsyncMock(return_value={"boot_epoch": "epoch"}),
            validate=AsyncMock(return_value={"valid": True}))
        self.helper.store = Mock()
        self.worker = dict(worker_id="mac", worker_incarnation="boot1")

    def message(self, operation="checkpoint.status", **changes):
        value = dict(type=operation, id="request", service_id="controller",
            worker_id="mac", worker_incarnation="boot1", boot_epoch="epoch",
            binding=None, checkpoint=None)
        value.update(changes)
        return value

    async def test_every_remote_operation_rejects_another_or_missing_service_before_io(self):
        for operation in ("checkpoint.status", "checkpoint.get", "checkpoint.put",
                          "checkpoint.published", "checkpoint.chunk.get",
                          "checkpoint.chunk.put", "checkpoint.chunk.missing"):
            for service in ("relay", None):
                with self.subTest(operation=operation, service=service):
                    reply = await self.helper.worker_reply(
                        self.message(operation, service_id=service), self.worker)
                    self.assertEqual(403, reply["status"])
                    self.assertEqual("controller", reply["service_id"])
                    self.assertEqual({}, reply["result"])
        self.helper.transport.request.assert_not_awaited()
        self.helper.transport.validate.assert_not_awaited()
        self.assertEqual([], self.helper.store.mock_calls)

    async def test_foreign_supervisor_or_binding_never_accesses_the_scoped_store(self):
        for change in ({"worker_id": "pi"}, {"worker_incarnation": "old"},
                       {"binding": dict(BINDING, service_id="relay")}):
            with self.subTest(change=change):
                reply = await self.helper.worker_reply(self.message(**change), self.worker)
                self.assertEqual(403, reply["status"])
        self.helper.transport.request.assert_not_awaited()
        self.assertEqual([], self.helper.store.mock_calls)

    async def test_local_adapter_rejects_foreign_scope_before_chunk_or_phase_access(self):
        other = dict(BINDING, service_id="relay")
        for operation in ("chunk_missing", "chunk_put"):
            with self.subTest(operation=operation):
                with self.assertRaises(Fault) as caught:
                    await getattr(self.helper, operation)({"binding": other})
                self.assertEqual(403, caught.exception.status)
        request = types.SimpleNamespace(
            headers={"Authorization": "Bearer " + self.helper.token},
            path="/adapter", json=AsyncMock(return_value={"operation": "status", "binding": other}))
        response = await self.helper.handle(request)
        self.assertEqual(403, response.status)
        self.helper.transport.validate.assert_not_awaited()
        self.assertEqual([], self.helper.store.mock_calls)
        await self.helper.validate(BINDING)
        self.helper.transport.validate.assert_awaited_once_with(BINDING)

    async def test_valid_status_echoes_service_and_preserves_empty_store_semantics(self):
        self.helper.store.packet.return_value = None
        self.helper.store.head.return_value = None
        self.helper.store.retained.return_value = []
        reply = await self.helper.worker_reply(self.message(), self.worker)
        self.assertEqual(200, reply["status"])
        self.assertEqual("controller", reply["service_id"])
        self.assertIsNone(reply["checkpoint"])
        self.assertEqual({"durable": True, "retained": []}, reply["result"])


class Socket:
    def __init__(self):
        self.sent = []
        self.ack = asyncio.get_running_loop().create_future()
        self.opened, self.closed_event = asyncio.Event(), asyncio.Event()
        self.closed = False

    async def __aenter__(self):
        self.opened.set()
        return self

    async def __aexit__(self, *unused):
        self.closed = True
        self.closed_event.set()

    async def send_json(self, value):
        self.sent.append(value)

    async def receive_json(self, timeout):
        return await asyncio.wait_for(self.ack, timeout)

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.get_running_loop().create_future()

    async def close(self):
        self.closed = True


class HelperRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.helper = CheckpointHelper.__new__(CheckpointHelper)
        self.helper.config = dict(service_id="controller", worker_id="mac", request_timeout_s=.1)
        self.worker = dict(worker_id="mac", worker_incarnation="boot1")
        self.helper.workers = AsyncMock(return_value=({"boot_epoch": "epoch"}, {"mac": self.worker}))
        self.helper.transport = types.SimpleNamespace(origin="https://fixture.invalid", key="fixture-key")
        self.helper.connection = None
        self.socket = Socket()
        self.helper.session = types.SimpleNamespace(ws_connect=Mock(return_value=self.socket))
        self.monitored = asyncio.Event()

        async def monitor(*unused):
            self.monitored.set()
            await asyncio.get_running_loop().create_future()
        self.helper.watch_incarnation = monitor
        self.task = asyncio.create_task(self.helper.worker_loop())
        self.addAsyncCleanup(self.stop)
        await asyncio.wait_for(self.socket.opened.wait(), 1)
        await asyncio.sleep(0)

    async def stop(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    def ack(self, **changes):
        value = dict(type="registered", protocol=PROTOCOL, service_id="controller",
                     worker_id="mac", worker_incarnation="boot1", boot_epoch="epoch")
        value.update(changes)
        return value

    async def test_registration_is_explicit_and_health_is_unready_until_exact_ack(self):
        self.assertEqual([dict(type="register", service_id="controller", **self.worker)], self.socket.sent)
        self.assertIsNone(self.helper.connection)
        self.socket.ack.set_result(self.ack())
        await asyncio.wait_for(self.monitored.wait(), 1)
        self.assertEqual(self.worker, self.helper.connection)

    async def test_missing_scope_ack_from_old_gateway_is_not_accepted(self):
        ack = self.ack()
        ack.pop("service_id")
        self.socket.ack.set_result(ack)
        await asyncio.wait_for(self.socket.closed_event.wait(), 1)
        self.assertIsNone(self.helper.connection)
        self.assertFalse(self.monitored.is_set())

    async def test_foreign_scope_ack_is_not_accepted(self):
        self.socket.ack.set_result(self.ack(service_id="relay"))
        await asyncio.wait_for(self.socket.closed_event.wait(), 1)
        self.assertIsNone(self.helper.connection)

    async def test_foreign_incarnation_ack_is_not_accepted(self):
        self.socket.ack.set_result(self.ack(worker_incarnation="old"))
        await asyncio.wait_for(self.socket.closed_event.wait(), 1)
        self.assertIsNone(self.helper.connection)

    async def test_changed_coordinator_epoch_ack_is_not_accepted(self):
        self.socket.ack.set_result(self.ack(boot_epoch="changed"))
        await asyncio.wait_for(self.socket.closed_event.wait(), 1)
        self.assertIsNone(self.helper.connection)

    async def test_registration_deadline_leaves_health_unready(self):
        await asyncio.wait_for(self.socket.closed_event.wait(), 1)
        self.assertIsNone(self.helper.connection)
        self.assertFalse(self.monitored.is_set())


if __name__ == "__main__":
    unittest.main()
