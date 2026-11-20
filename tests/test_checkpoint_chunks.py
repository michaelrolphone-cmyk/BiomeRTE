"""Durability, corruption, bounded publication and transactional restore tests."""
import base64
import copy
import hashlib
import json
import os
import pathlib
import tempfile
import types
import unittest

from biomerte.checkpoint_chunks import (
    BLOCK_BYTES, STATE_FORMAT, ChunkStore, ChunkError, canonical,
    describe_shard, iter_records, state_blocks)
from biomerte.controller_checkpoint import publish_controller
from biomerte.service_checkpoint import CheckpointStore, CheckpointError, checkpoint_ref

B = dict(service_id="controller", manifest_revision="r1", worker_id="mac",
    worker_incarnation="mac-1", boot_epoch="epoch-1", grant_token="grant-1")


def shard(store, value="value", key="artifact-1", kind="artifacts"):
    raw = canonical([[kind, key, value]])
    prefix = hashlib.sha256(key.encode()).hexdigest()[:2]
    return store.write_shard(kind, prefix, raw)


def root(shards):
    return dict(format=STATE_FORMAT, authority="authority", site="site", shards=shards)


class Blocks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "checkpoint.sqlite")
        self.store = CheckpointStore(self.path, "controller", "scope", b"k" * 32)
        self.addCleanup(self.store.close)

    def test_partial_upload_never_acknowledges_a_root(self):
        raw = canonical([["artifacts", "artifact-1", "x" * (BLOCK_BYTES * 2)]])
        desc = describe_shard("artifacts", hashlib.sha256(b"artifact-1").hexdigest()[:2], raw)
        self.store.chunks.put(raw[:BLOCK_BYTES])
        with self.assertRaises(CheckpointError):
            self.store.bootstrap(root([desc]), {"source_receipt": "frozen-fixture"})
        self.assertIsNone(self.store.head())
        for offset in range(BLOCK_BYTES, len(raw), BLOCK_BYTES):
            self.store.chunks.put(raw[offset:offset + BLOCK_BYTES])
        packet = self.store.bootstrap(root([desc]), {"source_receipt": "frozen-fixture"})
        reopened = CheckpointStore(self.path, "controller", "scope", b"k" * 32)
        try:
            reopened.require_chunks(packet["payload"]["state"])
            self.assertEqual(packet, reopened.packet())
        finally:
            reopened.close()

    def test_same_length_corruption_is_detected_after_verified_upload(self):
        desc = shard(self.store.chunks)
        state = root([desc])
        self.store.bootstrap(state, {"source_receipt": "fixture"})
        key = desc["blocks"][0]["digest"]
        path = pathlib.Path(self.store.chunks.directory) / key
        path.write_bytes(b"x" * path.stat().st_size)
        self.assertEqual([key], self.store.chunks.missing([key]))
        with self.assertRaises(CheckpointError):
            self.store.require_chunks(state)
        with self.assertRaises(ChunkError):
            list(iter_records(state, self.store.chunks.get))

    def test_upload_digest_mismatch_and_oversize_are_refused(self):
        with self.assertRaises(ChunkError):
            self.store.chunks.put(b"wrong", "a" * 64)
        with self.assertRaises(ChunkError):
            self.store.chunks.put(b"x" * (BLOCK_BYTES + 1))
        self.assertEqual([], os.listdir(self.store.chunks.directory))

    def test_root_signature_and_full_block_description_are_verified(self):
        desc = shard(self.store.chunks)
        packet = self.store.bootstrap(root([desc]), {"source_receipt": "fixture"})
        changed = copy.deepcopy(packet)
        changed["payload"]["state"]["shards"][0]["blocks"][0]["digest"] = "f" * 64
        with self.assertRaises(CheckpointError):
            self.store.install(changed)
        bad = copy.deepcopy(packet["payload"])
        bad["state"]["shards"][0]["bytes"] += 1
        with self.assertRaises(CheckpointError):
            self.store.install(self.store._sign(bad))

    def test_duplicate_shard_and_wrong_record_custody_are_rejected(self):
        desc = shard(self.store.chunks)
        with self.assertRaises(ChunkError):
            state_blocks(root([desc, desc]))
        wrong = dict(desc, prefix="00" if desc["prefix"] != "00" else "01")
        with self.assertRaises(ChunkError):
            list(iter_records(root([wrong]), self.store.chunks.get))

    def test_published_gc_keeps_latest_root_and_recent_uncommitted_uploads(self):
        initial = shard(self.store.chunks, "old")
        self.store.bootstrap(root([initial]), {"source_receipt": "fixture"})
        old = pathlib.Path(self.store.chunks.directory) / initial["blocks"][0]["digest"]
        os.utime(old, (0, 0))
        current = shard(self.store.chunks, "new")
        staged = self.store.chunks.put(b"unfinished-upload")["digest"]
        packet = self.store.advance(root([current]), B)
        self.store.published(checkpoint_ref(packet))
        self.assertFalse(old.exists())
        self.assertTrue(self.store.chunks.has(staged))
        self.assertEqual([checkpoint_ref(packet)], self.store.retained())
        self.assertEqual([["artifacts", "artifact-1", "new"]],
                         list(iter_records(packet["payload"]["state"], self.store.chunks.get)))

    def test_restore_is_exact_and_corruption_rolls_back_the_whole_replacement(self):
        try:
            from biomerte.service_daemon import restore_state
        except ModuleNotFoundError as error:
            if error.name == "aiohttp":
                self.skipTest("Optional helper restore integration requires aiohttp")
            raise
        from biomerte.store import Store
        home = pathlib.Path(self.directory.name) / "authority"
        private = pathlib.Path(self.directory.name) / "private"
        private.mkdir()
        (private / "node.json").write_text("{}")
        (private / "node.key").write_text("fixture")
        config = dict(authority_kind="controller", authority_id="authority",
                      site="site", service_id="controller", controller_aliases=[])
        first = root([shard(self.store.chunks, {"state": "paused"}, kind="assistant_responsibilities")])
        packet = self.store.bootstrap(first, {"source_receipt": "fixture"})
        restore_state(str(home), str(private), packet, config, self.store.chunks.get)
        local = Store(str(home), "site")
        try:
            self.assertEqual({"state": "paused"}, local.execute(
                lambda tx: tx.get("assistant_responsibilities", "artifact-1"), read_only=True))
        finally:
            local.close()
        self.assertEqual("managed-authority",
                         json.loads((home / "controller-authority.json").read_text())["mode"])
        desc = shard(self.store.chunks, "replacement")
        wrong = dict(desc, prefix="00" if desc["prefix"] != "00" else "01")
        corrupt = self.store._sign(dict(packet["payload"], sequence=1, state=root([wrong])))
        with self.assertRaises(ChunkError):
            restore_state(str(home), str(private), corrupt, config, self.store.chunks.get)
        local = Store(str(home), "site")
        try:
            self.assertEqual({"state": "paused"}, local.execute(
                lambda tx: tx.get("assistant_responsibilities", "artifact-1"), read_only=True))
            self.assertIsNone(local.execute(lambda tx: tx.get("artifacts", "artifact-1"), read_only=True))
        finally:
            local.close()

    def test_controller_restore_cache_is_local_and_keeps_durable_triggered_writes(self):
        try:
            from biomerte import service_daemon
        except ModuleNotFoundError as error:
            if error.name == "aiohttp":
                self.skipTest("Optional helper restore integration requires aiohttp")
            raise
        import contextlib
        import sqlite3
        from unittest.mock import patch
        from biomerte.store import Store

        home = pathlib.Path(self.directory.name) / "cached-authority"
        private = pathlib.Path(self.directory.name) / "cache-private"
        private.mkdir()
        (private / "node.json").write_text("{}")
        (private / "node.key").write_text("fixture")
        config = dict(authority_kind="controller", authority_id="authority",
                      site="site", service_id="controller", controller_aliases=[])
        packet = self.store.bootstrap(
            root([shard(self.store.chunks, {"state": "waiting"}, kind="runs")]),
            {"source_receipt": "fixture"})
        connections, observed = [], []

        class ObservedStore(Store):
            def connect(self):
                db = super().connect()
                connections.append(db)
                return db

        def read_block(key):
            # iter_records reads its first block after DELETE, inside the real
            # bulk transaction. Inspect that same live SQLite connection.
            db = connections[-1]
            observed.append(dict(
                cache=db.execute("PRAGMA cache_size").fetchone()[0],
                synchronous=db.execute("PRAGMA synchronous").fetchone()[0],
                journal=db.execute("PRAGMA journal_mode").fetchone()[0],
                spill=db.execute("PRAGMA cache_spill").fetchone()[0],
                transaction=db.in_transaction))
            return self.store.chunks.get(key)

        with patch.object(service_daemon, "Store", ObservedStore):
            service_daemon.restore_state(
                str(home), str(private), packet, config, read_block)
        self.assertTrue(observed)
        for settings in observed:
            self.assertEqual(-65536, settings["cache"])
            self.assertEqual(2, settings["synchronous"])
            self.assertEqual("wal", settings["journal"])
            self.assertGreater(settings["spill"], 0)
            self.assertTrue(settings["transaction"])
        with self.assertRaises(sqlite3.ProgrammingError):
            connections[-1].execute("SELECT 1")

        with contextlib.closing(sqlite3.connect(":memory:")) as ordinary:
            default_cache = ordinary.execute("PRAGMA cache_size").fetchone()[0]
        with contextlib.closing(sqlite3.connect(str(home / "site.sqlite"))) as db:
            # The bulk budget is not persisted or imposed on ordinary readers.
            self.assertEqual(default_cache, db.execute("PRAGMA cache_size").fetchone()[0])
            self.assertEqual([("runs", "artifact-1", '{"state":"waiting"}')],
                             db.execute("SELECT kind,id,value FROM records").fetchall())
            self.assertEqual("@dirty", db.execute(
                "SELECT state FROM record_lifecycle WHERE kind='runs' AND id='artifact-1'").fetchone()[0])
            self.assertIsNotNone(db.execute(
                "SELECT 1 FROM record_lookup_dirty WHERE kind='runs' AND id='artifact-1'").fetchone())
            self.assertIsNotNone(db.execute(
                "SELECT 1 FROM retention_ref_dirty WHERE kind='runs' AND id='artifact-1'").fetchone())


class Publisher(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.blocks = ChunkStore(self.directory.name)
        self.calls, self.reads = [], []
        self.raws = {}
        self.manifest = root([])
        self.bad_ack = False
        self.daemon = types.SimpleNamespace(authority=types.SimpleNamespace(
            snapshot=lambda: self.manifest, snapshot_chunk=self.read), helper=self.helper)

    def read(self, key):
        self.reads.append(key)
        return self.raws[key]

    def add(self, key, value):
        raw = canonical([["artifacts", key, value]])
        desc = describe_shard("artifacts", hashlib.sha256(key.encode()).hexdigest()[:2], raw)
        self.raws[desc["digest"]] = raw
        self.manifest["shards"].append({k: v for k, v in desc.items() if k != "blocks"})
        return desc

    async def helper(self, operation, **body):
        self.calls.append((operation, body))
        if operation == "chunk_missing":
            return {"missing": self.blocks.missing(body["digests"])}
        if operation == "chunk_put":
            block = body["block"]
            raw = base64.b64decode(block["data"], validate=True)
            receipt = self.blocks.put(raw, block["digest"])
            return dict(receipt, durable=False) if self.bad_ack else receipt
        if operation == "publish":
            self.blocks.require(body["state"])
            return {"checkpoint": {"digest": "c" * 64}, "replication": "complete"}
        raise AssertionError(operation)

    async def test_large_shard_uses_bounded_packets_and_unchanged_state_reads_no_bodies(self):
        desc = self.add("large", "x" * (33 * 1024 * 1024))
        await publish_controller(self.daemon)
        self.assertEqual([desc["digest"]], self.reads)
        uploads = [body["block"] for operation, body in self.calls if operation == "chunk_put"]
        self.assertTrue(uploads)
        self.assertTrue(all(len(base64.b64decode(block["data"])) <= BLOCK_BYTES for block in uploads))
        # Identical full blocks deduplicate even within the same large shard.
        self.assertLess(len(uploads), len(desc["blocks"]))
        self.calls.clear()
        await publish_controller(self.daemon)
        self.assertEqual([desc["digest"]], self.reads)
        self.assertEqual(["publish"], [operation for operation, unused in self.calls])

    async def test_incremental_snapshot_uploads_only_changed_shard(self):
        old = self.add("one", "first")
        await publish_controller(self.daemon)
        self.calls.clear()
        new = self.add("two", "second")
        await publish_controller(self.daemon)
        self.assertEqual([old["digest"], new["digest"]], self.reads)
        uploaded = [body["block"]["digest"] for operation, body in self.calls if operation == "chunk_put"]
        self.assertEqual([new["blocks"][0]["digest"]], uploaded)

    async def test_lost_or_false_durable_ack_prevents_root_publication_and_retry_reconciles(self):
        self.add("one", "value")
        self.bad_ack = True
        with self.assertRaises(CheckpointError):
            await publish_controller(self.daemon)
        self.assertNotIn("publish", [operation for operation, unused in self.calls])
        self.bad_ack = False
        self.calls.clear()
        await publish_controller(self.daemon)
        self.assertEqual(["chunk_missing", "publish"], [operation for operation, unused in self.calls])


if __name__ == "__main__":
    unittest.main()
