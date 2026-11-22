"""Durability, corruption, bounded publication and transactional restore tests."""
import asyncio
import base64
import copy
import contextlib
import hashlib
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

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



class ControllerRestoreModes(unittest.TestCase):
    """Exercise complete restores, interruption recovery and unchanged-row behavior."""

    def setUp(self):
        try:
            from biomerte import service_daemon
        except ModuleNotFoundError as error:
            if error.name == "aiohttp":
                self.skipTest("Optional helper restore integration requires aiohttp")
            raise
        from biomerte.store import Store, Records
        self.restore = service_daemon.restore_state
        self.Store, self.Records = Store, Records
        self.source = pathlib.Path(service_daemon.__file__).resolve().parents[1]
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = pathlib.Path(self.directory.name)
        self.home, self.private = self.path / "authority", self.path / "private"
        self.private.mkdir()
        (self.private / "node.json").write_text("{}")
        (self.private / "node.key").write_text("fixture")
        self.chunks = ChunkStore(str(self.path / "chunks"))
        self.config = dict(authority_kind="controller", authority_id="authority",
                           site="site", service_id="controller", controller_aliases=[])
        self.marker = self.home / "controller-authority.json"

    def packet(self, rows):
        groups = {}
        for row in rows:
            prefix = hashlib.sha256(row[1].encode()).hexdigest()[:2]
            groups.setdefault((row[0], prefix), []).append(row)
        descriptors = [self.chunks.write_shard(kind, prefix,
                       canonical(sorted(values, key=lambda row: row[1])))
                       for (kind, prefix), values in sorted(groups.items())]
        return {"payload": {"state": root(descriptors)}}

    def apply(self, packet, reader=None):
        self.restore(str(self.home), str(self.private), packet, self.config,
                     self.chunks.get if reader is None else reader)

    def observe(self, packet):
        connections, observed = [], []
        original = self.Store.connect

        def connect(store):
            db = original(store)
            connections.append(db)
            return db

        def read(key):
            if not observed:
                db = connections[-1]
                observed.append(dict(
                    mode=db.execute("PRAGMA journal_mode").fetchone()[0],
                    synchronous=db.execute("PRAGMA synchronous").fetchone()[0],
                    temp_store=db.execute("PRAGMA temp_store").fetchone()[0],
                    temp_cache=(db.execute("PRAGMA temp.cache_size").fetchone()[0]
                                if any(row[1] == "temp" for row in db.execute("PRAGMA database_list"))
                                else None),
                    in_transaction=db.in_transaction))
            return self.chunks.get(key)

        with patch.object(self.Store, "connect", connect):
            self.apply(packet, read)
        for db in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute("SELECT 1")
        return observed[0]

    def database(self):
        return sqlite3.connect(str(self.home / "site.sqlite"), isolation_level=None)

    def snapshot(self):
        with contextlib.closing(self.database()) as db:
            return dict(records=db.execute("SELECT kind,id,value FROM records ORDER BY kind,id").fetchall(),
                        position=db.execute("SELECT seq,hash FROM position WHERE id=1").fetchone(),
                        marker=self.marker.read_bytes() if self.marker.exists() else None)

    def assert_rows(self, rows):
        expected = sorted((kind, key, canonical(value).decode()) for kind, key, value in rows)
        self.assertEqual(expected, self.snapshot()["records"])

    def test_fresh_load_is_extra_durable_and_returns_to_normal_store_modes(self):
        rows = [["actions", "terminal", {"state": "succeeded", "destination": "owner"}],
                ["assistant_responsibilities", "paused", {"state": "paused"}],
                ["artifacts", "unicode", {"text": "\u2603", "values": [1, False, None]}]]
        observed = self.observe(self.packet(rows))
        self.assertEqual(("delete", 3, True),
                         (observed["mode"], observed["synchronous"], observed["in_transaction"]))
        self.assert_rows(rows)  # A terminal action must not acquire new-write expiry defaults.
        self.assertEqual((0, ""), self.snapshot()["position"])
        self.assertEqual("managed-authority", json.loads(self.marker.read_text())["mode"])
        local = self.Store(str(self.home), "site")
        try:
            with contextlib.closing(local.connect()) as db:
                self.assertEqual("wal", db.execute("PRAGMA journal_mode").fetchone()[0])
                self.assertEqual(2, db.execute("PRAGMA synchronous").fetchone()[0])
                self.assertEqual(0, db.execute("PRAGMA temp_store").fetchone()[0])
                self.assertEqual([], db.execute("SELECT name FROM sqlite_temp_master").fetchall())
                self.assertEqual([("@dirty", "", None, 0)] * len(rows), db.execute(
                    "SELECT state,lease,expires_at,delivery FROM record_lifecycle").fetchall())
        finally:
            local.close()

    def test_late_corruption_rolls_back_empty_target_and_retry_still_uses_fresh_path(self):
        rows = [["artifacts", "first", "accepted-before-error"],
                ["runs", "last", {"state": "ready"}]]
        packet = self.packet(rows)
        bad = packet["payload"]["state"]["shards"][-1]["blocks"][0]["digest"]

        def corrupt(key):
            return b"!" if key == bad else self.chunks.get(key)

        with self.assertRaises(ChunkError):
            self.apply(packet, corrupt)
        self.assertEqual([], self.snapshot()["records"])
        self.assertFalse(self.marker.exists())
        observed = self.observe(packet)
        self.assertEqual(("delete", 3), (observed["mode"], observed["synchronous"]))
        self.assert_rows(rows)

    def test_populated_restore_only_changes_different_or_absent_canonical_rows(self):
        unchanged = ["artifacts", "same", {"text": "x" * 70000}]
        rows = [unchanged, ["artifacts", "change", {"old": True}],
                ["artifacts", "remove", {"reference": "obsolete"}],
                ["actions", "terminal", {"state": "succeeded"}],
                ["runs", "status", {"state": "waiting"}]]
        self.apply(self.packet(rows))
        # Repair the normal lookup projection before measuring the next restore.
        local = self.Store(str(self.home), "site")
        local.close()
        with contextlib.closing(self.database()) as db:
            db.executescript(
                "CREATE TABLE canonical_changes(event TEXT,kind TEXT,id TEXT);"
                "CREATE TRIGGER audit_insert AFTER INSERT ON records BEGIN "
                "INSERT INTO canonical_changes VALUES('insert',NEW.kind,NEW.id); END;"
                "CREATE TRIGGER audit_update AFTER UPDATE ON records BEGIN "
                "INSERT INTO canonical_changes VALUES('update',NEW.kind,NEW.id); END;"
                "CREATE TRIGGER audit_delete AFTER DELETE ON records BEGIN "
                "INSERT INTO canonical_changes VALUES('delete',OLD.kind,OLD.id); END;"
                "CREATE TABLE lifecycle_changes(kind TEXT,id TEXT);"
                "CREATE TRIGGER audit_lifecycle AFTER UPDATE ON record_lifecycle BEGIN "
                "INSERT INTO lifecycle_changes VALUES(NEW.kind,NEW.id); END;")
            db.execute("UPDATE record_lifecycle SET state='succeeded',expires_at=1 WHERE kind='actions'")
            db.execute("UPDATE record_lifecycle SET state=NULL WHERE kind='artifacts' AND id='same'")
            db.execute("DELETE FROM lifecycle_changes")
            db.execute("UPDATE position SET seq=17,hash='stale-position'")
            for key in ("same", "change", "remove"):
                db.execute("INSERT OR REPLACE INTO retention_ref_progress VALUES(?,?,123,1,0,'partial',0)",
                           ("artifacts", key))
            self.assertEqual([], db.execute("SELECT * FROM record_lookup_dirty").fetchall())
        expected = [unchanged, ["artifacts", "change", {"new": True}],
                    ["artifacts", "new", "added"],
                    ["actions", "terminal", {"state": "succeeded"}],
                    ["runs", "status", {"state": "ready"}]]
        observed = self.observe(self.packet(expected))
        self.assertEqual(("wal", 2, 1, -2048, True),
                         (observed["mode"], observed["synchronous"], observed["temp_store"],
                          observed["temp_cache"], observed["in_transaction"]))
        self.assert_rows(expected)
        self.assertEqual((0, ""), self.snapshot()["position"])
        with contextlib.closing(self.database()) as db:
            self.assertEqual([
                ("delete", "artifacts", "remove"),
                ("insert", "artifacts", "new"),
                ("update", "artifacts", "change"),
                ("update", "runs", "status")],
                db.execute("SELECT * FROM canonical_changes ORDER BY event,kind,id").fetchall())
            self.assertEqual([("actions", "terminal"), ("artifacts", "same")],
                             db.execute("SELECT * FROM lifecycle_changes ORDER BY kind,id").fetchall())
            self.assertEqual([("@dirty", "", None, 0)] * len(expected), db.execute(
                "SELECT state,lease,expires_at,delivery FROM record_lifecycle").fetchall())
            self.assertEqual([("artifacts", "same", 123)], db.execute(
                "SELECT kind,id,offset FROM retention_ref_progress").fetchall())
            self.assertEqual([("runs", "status")],
                             db.execute("SELECT * FROM record_lookup_dirty").fetchall())
            self.assertEqual([{"state": "ready"}],
                             self.Records(db).matching("runs", "state", ["ready"]))
            self.assertEqual([], self.Records(db).matching("runs", "state", ["waiting"]))
            # Changed/deleted reference projections must stay fail closed until rebuilt.
            self.assertIn(("artifacts", "remove"),
                          db.execute("SELECT * FROM retention_ref_dirty").fetchall())

    def test_null_raw_value_cannot_be_mistaken_for_unchanged_canonical_data(self):
        rows = [["artifacts", "nullable", None]]
        self.apply(self.packet(rows))
        with contextlib.closing(self.database()) as db:
            db.execute("UPDATE records SET value=NULL WHERE kind='artifacts'")
        self.apply(self.packet(rows))
        self.assert_rows(rows)
        self.assertEqual("null", self.snapshot()["records"][0][2])

    def test_bad_late_input_preserves_prior_records_position_and_marker(self):
        baseline = [["artifacts", "keep", {"original": True}],
                    ["actions", "pending", {"state": "pending"}]]
        self.apply(self.packet(baseline))
        with contextlib.closing(self.database()) as db:
            db.execute("UPDATE position SET seq=23,hash='prior-durable-position'")
        before = self.snapshot()
        for failure in ("corrupt-block", "duplicate-id"):
            with self.subTest(failure=failure):
                rows = [["artifacts", "new", {"changed-before-error": True}],
                        ["runs", "late", {"state": "ready"}]]
                if failure == "duplicate-id":
                    rows.append(["runs", "late", {"state": "ready"}])
                packet = self.packet(rows)
                bad = packet["payload"]["state"]["shards"][-1]["blocks"][0]["digest"]

                def read(key):
                    return b"!" if failure == "corrupt-block" and key == bad else self.chunks.get(key)

                with self.assertRaises(ChunkError):
                    self.apply(packet, read)
                self.assertEqual(before, self.snapshot())

    def test_foreign_marker_rejects_restore_before_existing_canonical_changes(self):
        baseline = [["artifacts", "kept", {"value": "original"}]]
        self.apply(self.packet(baseline))
        marker = json.loads(self.marker.read_text())
        marker["authority_id"] = "foreign"
        self.marker.write_bytes(canonical(marker))
        before = self.snapshot()
        with self.assertRaisesRegex(CheckpointError, "marker differs"):
            self.apply(self.packet([["artifacts", "new", "unaccepted"]]))
        self.assertEqual(before, self.snapshot())


    def test_failed_return_to_wal_cannot_publish_marker_and_retry_is_exact(self):
        rows = [["artifacts", "committed", {"body": "durable-but-not-prepared"}]]
        packet = self.packet(rows)
        original, connections = self.Store.connect, []

        class TransitionFailure:
            def __init__(self, connection):
                self.connection = connection

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def execute(self, statement, *args):
                if statement == "PRAGMA journal_mode=WAL":
                    return types.SimpleNamespace(fetchone=lambda: ("delete",))
                return self.connection.execute(statement, *args)

        def connect(store):
            db = original(store)
            connections.append(db)
            # The first connection is ordinary Store initialization. Simulate
            # failure only at the bulk connection's post-commit transition.
            return db if len(connections) == 1 else TransitionFailure(db)

        with patch.object(self.Store, "connect", connect):
            with self.assertRaisesRegex(CheckpointError, "return to WAL"):
                self.apply(packet)
        self.assertFalse(self.marker.exists())
        self.assert_rows(rows)
        observed = self.observe(packet)
        self.assertEqual(("wal", 2), (observed["mode"], observed["synchronous"]))
        self.assert_rows(rows)
        self.assertTrue(self.marker.is_file())

    def test_process_exit_recovers_hot_journal_without_manual_file_cleanup(self):
        # A spilled first row leaves a real rollback journal when Python cannot
        # run exception/finally cleanup. The next normal Store must recover it.
        rows = [["artifacts", "large", {"body": "x" * (8 * 1024 * 1024)}],
                ["runs", "after-crash", {"state": "ready"}]]
        packet = self.packet(rows)
        packet_path = self.path / "packet.json"
        packet_path.write_bytes(canonical(packet))
        stop_digest = packet["payload"]["state"]["shards"][-1]["blocks"][0]["digest"]
        program = (
            "import json,os,pathlib,sys\n"
            "from biomerte.checkpoint_chunks import ChunkStore\n"
            "from biomerte.service_daemon import restore_state\n"
            "home,private,packet_path,chunk_path,stop=sys.argv[1:]\n"
            "chunks=ChunkStore(chunk_path)\n"
            "def read(key):\n"
            "    if key==stop: os._exit(73)\n"
            "    return chunks.get(key)\n"
            "config=" + repr(self.config) + "\n"
            "restore_state(home,private,json.loads(pathlib.Path(packet_path).read_text()),config,read)\n")
        result = subprocess.run([sys.executable, "-B", "-c", program,
            str(self.home), str(self.private), str(packet_path), self.chunks.directory, stop_digest],
            cwd=str(self.source), capture_output=True, text=True, timeout=40)
        self.assertEqual(73, result.returncode, result.stderr)
        journal = self.home / "site.sqlite-journal"
        self.assertTrue(journal.is_file())
        self.assertGreater(journal.stat().st_size, 0)
        self.assertGreater((self.home / "site.sqlite").stat().st_size, 128 * 1024)
        self.assertFalse(self.marker.exists())
        observed = self.observe(packet)
        self.assertEqual(("delete", 3), (observed["mode"], observed["synchronous"]))
        self.assertFalse(journal.exists())
        self.assert_rows(rows)


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
        self.assertEqual([desc["digest"], desc["digest"]], self.reads)
        uploads = [body["block"] for operation, body in self.calls if operation == "chunk_put"]
        self.assertTrue(uploads)
        self.assertTrue(all(len(base64.b64decode(block["data"])) <= BLOCK_BYTES for block in uploads))
        # Identical full blocks deduplicate even within the same large shard.
        self.assertLess(len(uploads), len(desc["blocks"]))
        self.calls.clear()
        await publish_controller(self.daemon)
        self.assertEqual([desc["digest"], desc["digest"]], self.reads)
        self.assertEqual(["publish"], [operation for operation, unused in self.calls])

    async def test_incremental_snapshot_uploads_only_changed_shard(self):
        old = self.add("one", "first")
        await publish_controller(self.daemon)
        self.calls.clear()
        new = self.add("two", "second")
        await publish_controller(self.daemon)
        self.assertEqual([old["digest"], old["digest"], new["digest"], new["digest"]], self.reads)
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

    async def test_overlapping_block_uploads_finish_before_publishing_root(self):
        for index in range(6):
            self.add("key-" + str(index), "unique value " + str(index))
        current = 0
        peak = 0
        saturated = asyncio.Event()
        release = asyncio.Event()

        async def helper(operation, **body):
            nonlocal current, peak
            if operation == "chunk_put":
                current += 1
                peak = max(peak, current)
                if current == 4:
                    saturated.set()
                try:
                    await release.wait()
                    return await self.helper(operation, **body)
                finally:
                    current -= 1
            if operation == "publish":
                self.assertEqual(current, 0)
            return await self.helper(operation, **body)

        self.daemon.helper = helper
        publication = asyncio.create_task(publish_controller(self.daemon))
        try:
            await asyncio.wait_for(saturated.wait(), 2)
            self.assertFalse(publication.done())
            self.assertFalse(any(operation == "publish" for operation, unused in self.calls))
        finally:
            release.set()
            await publication
        self.assertEqual(peak, 4)
        self.assertEqual(self.calls[-1][0], "publish")

    async def test_cold_4050_shard_4405_block_root_uses_18_bounded_queries(self):
        # Use the real 256 KiB block size and generate large shard bodies on
        # demand, so the production-sized descriptor fixture stays bounded.
        self.assertEqual(BLOCK_BYTES, 256 * 1024)
        expected, indexes = [], {}
        def raw_for(index):
            kind, key = "cold-%04d" % index, "record-%04d" % index
            value = str(index) + ("x" * BLOCK_BYTES if index < 355 else "") + str(index)
            return canonical([[kind, key, value]])
        for index in range(4050):
            kind, key = "cold-%04d" % index, "record-%04d" % index
            desc = describe_shard(kind, hashlib.sha256(key.encode()).hexdigest()[:2],
                                  raw_for(index))
            expected.append(desc)
            indexes[desc["digest"]] = index
        expected_root = root(expected)
        refs = state_blocks(expected_root)
        self.assertEqual(len(refs), 4405)
        self.manifest = root([{k: v for k, v in desc.items() if k != "blocks"}
                              for desc in expected])
        def read(key):
            self.reads.append(key)
            return raw_for(indexes[key])
        self.daemon.authority.snapshot_chunk = read
        async def seeded_helper(operation, **body):
            self.calls.append((operation, body))
            if operation == "chunk_missing":
                self.assertTrue(set(body["digests"]).issubset(refs))
                return {"missing": []}
            self.assertEqual(operation, "publish")
            self.assertEqual(body["state"], expected_root)
            return {"checkpoint": {"digest": "c" * 64}, "replication": "complete"}
        self.daemon.helper = seeded_helper
        await publish_controller(self.daemon)
        queries = [body["digests"] for operation, body in self.calls
                   if operation == "chunk_missing"]
        self.assertEqual(len(queries), 18)
        self.assertEqual([len(query) for query in queries], [256] * 17 + [53])
        asked = [key for query in queries for key in query]
        self.assertEqual(len(asked), len(set(asked)))
        self.assertEqual(set(asked), set(refs))
        self.assertEqual(len(self.reads), 4050)
        self.assertEqual([operation for operation, unused in self.calls].count("publish"), 1)
        self.calls.clear()
        await publish_controller(self.daemon)
        self.assertEqual(len(self.reads), 4050)
        self.assertEqual(["publish"], [operation for operation, unused in self.calls])

    async def test_missing_subset_and_shared_blocks_upload_once_across_shards(self):
        first = self.add("one", "x" * (BLOCK_BYTES * 3))
        second = self.add("two", "x" * (BLOCK_BYTES * 3))
        complete = self.add("three", "already present")
        descriptors = [first, second, complete]
        shared = set(block["digest"] for block in first["blocks"]) & set(
            block["digest"] for block in second["blocks"])
        self.assertTrue(shared)
        missing = {next(iter(shared)), first["blocks"][0]["digest"]}
        refs = state_blocks(root(descriptors))
        for desc in descriptors:
            raw = self.raws[desc["digest"]]
            for index, block in enumerate(desc["blocks"]):
                if block["digest"] not in missing:
                    self.blocks.put(raw[index * BLOCK_BYTES:(index + 1) * BLOCK_BYTES],
                                    block["digest"])
        await publish_controller(self.daemon)
        queries = [body["digests"] for operation, body in self.calls
                   if operation == "chunk_missing"]
        self.assertEqual(len(queries), 1)
        self.assertEqual(len(queries[0]), len(refs))
        self.assertEqual(set(queries[0]), set(refs))
        uploads = [body["block"]["digest"] for operation, body in self.calls
                   if operation == "chunk_put"]
        self.assertEqual(len(uploads), len(missing))
        self.assertEqual(set(uploads), missing)
        self.assertEqual(self.reads.count(complete["digest"]), 1)
        self.blocks.require(root(descriptors))
        self.assertEqual(self.calls[-1], ("publish", {"state": root(descriptors)}))

    async def test_invalid_missing_reply_never_uploads_or_publishes(self):
        desc = self.add("one", "value")
        key = desc["blocks"][0]["digest"]
        for values in (None, "not a list", ["f" * 64], [key, key], [{}]):
            with self.subTest(values=values):
                self.calls.clear()
                self.daemon._controller_shard_blocks = {}
                async def invalid(operation, **body):
                    self.calls.append((operation, body))
                    self.assertEqual(operation, "chunk_missing")
                    return {"missing": values}
                self.daemon.helper = invalid
                with self.assertRaises(CheckpointError):
                    await publish_controller(self.daemon)
                self.assertEqual(["chunk_missing"], [operation for operation, unused in self.calls])
                self.assertEqual(self.daemon._controller_shard_blocks, {})

    async def test_changed_shard_between_description_and_upload_is_rejected(self):
        desc = self.add("one", "value")
        raw = self.raws[desc["digest"]]
        def changed(key):
            self.reads.append(key)
            return raw if len(self.reads) == 1 else b"!" + raw[1:]
        self.daemon.authority.snapshot_chunk = changed
        with self.assertRaisesRegex(CheckpointError, "shard changed"):
            await publish_controller(self.daemon)
        self.assertEqual(["chunk_missing"], [operation for operation, unused in self.calls])
        self.assertEqual(self.daemon._controller_shard_blocks, {})

    async def test_invalid_complete_root_fails_before_any_helper_rpc(self):
        self.add("one", "value")
        self.manifest["shards"].append(dict(self.manifest["shards"][0]))
        with self.assertRaisesRegex(ChunkError, "Distinct"):
            await publish_controller(self.daemon)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.daemon._controller_shard_blocks, {})

    async def test_lost_root_reply_clears_cache_and_retry_checks_existing_blocks(self):
        desc = self.add("one", "value")
        async def lost(operation, **body):
            result = await self.helper(operation, **body)
            if operation == "publish":
                raise OSError("Root reply lost")
            return result
        self.daemon.helper = lost
        with self.assertRaises(OSError):
            await publish_controller(self.daemon)
        self.assertEqual(self.daemon._controller_shard_blocks, {})
        self.calls.clear()
        self.daemon.helper = self.helper
        await publish_controller(self.daemon)
        self.assertEqual(["chunk_missing", "publish"], [operation for operation, unused in self.calls])
        self.assertEqual(self.daemon._controller_shard_blocks[desc["digest"]], desc["blocks"])

    async def test_real_helper_revalidates_grant_between_batches_and_rejects_wrong_host(self):
        from biomerte.service_daemon import CheckpointHelper
        from biomerte.store import Fault
        for index in range(257):
            kind, key = "grant-%d" % index, "record-%d" % index
            raw = canonical([[kind, key, "value"]])
            desc = describe_shard(kind, hashlib.sha256(key.encode()).hexdigest()[:2], raw)
            self.raws[desc["digest"]] = raw
            self.manifest["shards"].append({k: v for k, v in desc.items() if k != "blocks"})
        helper = CheckpointHelper.__new__(CheckpointHelper)
        helper.config = {"worker_id": B["worker_id"], "service_id": B["service_id"]}
        helper.store = types.SimpleNamespace(chunks=types.SimpleNamespace(missing=lambda keys: []))
        validations = []
        async def validate(current):
            validations.append(current)
            if len(validations) == 3:
                raise Fault("Grant revoked between batches", 503)
        helper.transport = types.SimpleNamespace(validate=validate)
        sent_binding = dict(B)
        async def adapter(operation, **body):
            self.calls.append((operation, body))
            self.assertEqual(operation, "chunk_missing")
            return await helper.chunk_missing(dict(body, binding=sent_binding))
        self.daemon.helper = adapter
        with self.assertRaisesRegex(Fault, "revoked between batches"):
            await publish_controller(self.daemon)
        self.assertEqual([len(body["digests"]) for operation, body in self.calls], [256, 1])
        self.assertEqual(len(validations), 3)
        self.assertEqual(self.daemon._controller_shard_blocks, {})
        self.calls.clear()
        validations.clear()
        sent_binding["worker_id"] = "unrelated-host"
        with self.assertRaisesRegex(Fault, "another host or service"):
            await publish_controller(self.daemon)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(validations, [])
        self.assertEqual(self.daemon._controller_shard_blocks, {})


if __name__ == "__main__":
    unittest.main()
