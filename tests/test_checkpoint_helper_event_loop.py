"""Checkpoint mutex contention must not stop the helper's HTTP event loop."""
import asyncio
import base64
import concurrent.futures
import json
import pathlib
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from biomerte.service_checkpoint import CheckpointStore
from biomerte.service_daemon import CheckpointHelper


BINDING = dict(service_id="controller", manifest_revision="a" * 64,
    worker_id="mac", worker_incarnation="boot1", boot_epoch="epoch",
    grant_token="isolated-grant")


class CheckpointHelperEventLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = CheckpointStore(
            str(pathlib.Path(self.directory.name) / "checkpoint.sqlite"),
            "controller", "b" * 64, b"isolated-checkpoint-key" * 2)
        self.addCleanup(self.store.close)
        self.raw = b"verified checkpoint block"
        self.block = self.store.chunks.put(self.raw)
        self.helper = CheckpointHelper.__new__(CheckpointHelper)
        self.helper.store = self.store
        self.helper.config = dict(authority_kind="controller",
            service_id="controller", worker_id="mac", replica_workers=["mac"],
            request_timeout_s=15, authority_home="/isolated/authority",
            private_dir="/isolated/identity")
        self.worker = dict(worker_id="mac", worker_incarnation="boot1")
        self.helper.connection = self.worker
        self.helper.replication = "degraded"
        self.reference = dict(service_id="controller", sequence=27, digest="c" * 64)
        self.helper.health_proof = (self.reference, time.monotonic() + 60)
        self.helper.lock = asyncio.Lock()
        self.helper.validate = AsyncMock(return_value={"valid": True})
        self.helper.transport = types.SimpleNamespace(
            request=AsyncMock(return_value={"boot_epoch": "epoch"}),
            validate=AsyncMock(return_value={"valid": True}))

    async def assert_health_while_checkpoint_locked(self, operation):
        """Probe actual health while a real CheckpointStore mutex is contended."""
        loop = asyncio.get_running_loop()
        locked, lookup_started = threading.Event(), threading.Event()
        outcome = {}
        original = CheckpointStore.chunks

        def observed_lookup(store):
            if store is self.store:
                lookup_started.set()
            return original.fget(store)

        def publisher():
            # Publication/pruning use this same real checkpoint-store lock.
            with self.store.lock:
                locked.set()
                if not lookup_started.wait(3):
                    outcome["error"] = "chunk lookup did not start"
                    return
                probe = asyncio.run_coroutine_threadsafe(self.helper.health(None), loop)
                try:
                    response = probe.result(timeout=3)
                    outcome["status"] = response.status
                    outcome["body"] = json.loads(response.body)
                except concurrent.futures.TimeoutError:
                    outcome["error"] = "health event loop blocked by checkpoint mutex"
                    probe.cancel()

        publisher_thread = threading.Thread(target=publisher, daemon=True)
        publisher_thread.start()
        self.assertTrue(await asyncio.to_thread(locked.wait, 3))
        try:
            with patch.object(CheckpointStore, "chunks", property(observed_lookup)):
                result = await operation()
        finally:
            await asyncio.to_thread(publisher_thread.join, 4)
        self.assertFalse(publisher_thread.is_alive())
        self.assertEqual(outcome.get("status"), 200, outcome)
        self.assertTrue(outcome["body"]["ready"])
        return result

    async def test_local_missing_block_query_keeps_http_health_responsive(self):
        result = await self.assert_health_while_checkpoint_locked(
            lambda: self.helper.chunk_missing(
                {"binding": BINDING, "digests": [self.block["digest"]]}))
        self.assertEqual(result, {"missing": []})
        self.assertEqual(self.helper.validate.await_count, 2)

    async def test_remote_get_put_and_missing_keep_http_health_responsive(self):
        for operation in ("get", "put", "missing"):
            with self.subTest(operation=operation):
                message = dict(type="checkpoint.chunk." + operation,
                    id="request-" + operation, service_id="controller",
                    worker_id="mac", worker_incarnation="boot1",
                    boot_epoch="epoch", binding=BINDING)
                if operation == "missing":
                    message["digests"] = [self.block["digest"]]
                else:
                    message["chunk"] = {"digest": self.block["digest"]}
                    if operation == "put":
                        message["chunk"]["data"] = base64.b64encode(self.raw).decode("ascii")
                result = await self.assert_health_while_checkpoint_locked(
                    lambda message=message: self.helper.worker_reply(message, self.worker))
                self.assertEqual(result["status"], 200, result)
                if operation == "get":
                    self.assertEqual(base64.b64decode(result["result"]["data"]), self.raw)
                elif operation == "put":
                    self.assertTrue(result["result"]["durable"])
                else:
                    self.assertEqual(result["result"], {"missing": []})

    async def test_prepare_defers_restore_reader_lookup_to_worker(self):
        packet = {"payload": {"state": {}}, "digest": self.reference["digest"]}
        request = dict(protocol="biomerte-service-adapter/1",
            operation="prepare", id="prepare-request", binding=BINDING)
        expected = {"prepared": True}
        self.helper.status = AsyncMock(return_value=(
            {"head": dict(self.reference, boot_epoch="epoch"), "workers": []}, {}))
        self.helper.fetch_chunks = AsyncMock()
        self.helper.publish_packet = AsyncMock()
        replacements = dict(receipt=Mock(return_value=None),
            phase=Mock(return_value=None), head=Mock(return_value=self.reference),
            packet=Mock(return_value=packet), require_chunks=Mock(return_value=None),
            retained=Mock(return_value=[self.reference]), get=Mock(return_value=packet),
            retain=Mock(return_value=None), select_witnessed=Mock(return_value=None),
            prepare=Mock(return_value=expected))
        with patch.multiple(self.store, **replacements), \
                patch("biomerte.service_daemon.restore_state") as restore:
            result = await self.assert_health_while_checkpoint_locked(
                lambda: self.helper.prepare(request))
        self.assertEqual(result, expected)
        restore.assert_called_once()
        self.assertEqual(restore.call_args.args[:4],
            ("/isolated/authority", "/isolated/identity", packet, self.helper.config))
        self.helper.fetch_chunks.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
