"""Detached source copies preserve provenance without installing runtime heads."""
import base64
import copy
import hashlib
import importlib.util
import json
import pathlib
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "controller_source_transfer", ROOT / "deploy/controller-authority/source_chunks.py")
transfer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(transfer)
migration = transfer.migration


class Chunks:
    def __init__(self):
        self.data = {}
    def put(self, raw, expected_digest=None):
        key = hashlib.sha256(raw).hexdigest()
        if expected_digest is not None and key != expected_digest:
            raise ValueError("fixture digest mismatch")
        if not 0 < len(raw) <= transfer.BLOCK_BYTES:
            raise ValueError("fixture byte bound")
        self.data[key] = raw
        return {"digest": key, "bytes": len(raw), "durable": True}
    def get(self, key):
        if key not in self.data:
            raise FileNotFoundError(key)
        raw = self.data[key]
        if hashlib.sha256(raw).hexdigest() != key:
            raise ValueError("fixture corruption")
        return raw
    def has(self, key):
        return key in self.data and hashlib.sha256(self.data[key]).hexdigest() == key


class DetachedSourceTransfer(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory()
        cls.key = pathlib.Path(cls.keys.name) / "node.key"
        cls.public_key = transfer.identity.generate(str(cls.key))
        cls.node = transfer.identity.fingerprint(cls.public_key)

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = pathlib.Path(self.directory.name)
        self.home = self.root / "native"
        self.home.mkdir()
        (self.home / "node.key").write_bytes(self.key.read_bytes())
        (self.home / "node.json").write_text(json.dumps(
            {"node": self.node, "site": "fixture-site", "public_key": self.public_key}))
        self.database = self.root / "site.sqlite"
        # Deterministic high-entropy text forces multiple compressed blocks.
        raw = b"".join(hashlib.sha256(str(i).encode()).digest() for i in range(17000))
        self.value = {"state": "uncertain", "original_operation": "same-effect",
                      "body": base64.b64encode(raw).decode(), "unicode": "café"}
        with sqlite3.connect(str(self.database)) as db:
            db.execute("CREATE TABLE records(kind TEXT,id TEXT,value TEXT,PRIMARY KEY(kind,id))")
            db.execute("INSERT INTO records VALUES(?,?,?)",
                       ("actions", "original-action", json.dumps(self.value)))
            db.execute("INSERT INTO records VALUES(?,?,?)",
                       ("assistant_responsibilities", "worker", json.dumps({"desired": "pause"})))
        self.events = self.root / "events.sqlite"
        with sqlite3.connect(str(self.events)) as db:
            db.execute("CREATE TABLE subscriptions(id TEXT PRIMARY KEY,args TEXT,key TEXT,cursor INTEGER)")
            db.execute("CREATE TABLE receiver_receipts(id TEXT PRIMARY KEY,signature TEXT,result TEXT)")
            db.execute("INSERT INTO subscriptions VALUES(?,?,?,?)",
                       ("original-sub", '{"realm":"site"}', "fixture-private-key", 7))
            db.execute("INSERT INTO receiver_receipts VALUES('original-receipt','same-signature','{}')")
        self.backup = self.root / "source-backup.json"
        metadata = migration.backup(self.database, self.root / "copied.sqlite", self.node,
                                    "fixture-site", self.events)
        migration.private_json(self.backup, metadata)
        self.chunks = Chunks()
        self.packet = transfer.pack_backup(self.backup, self.home, self.chunks)
        self.manifest = self.chunks.put(migration.canonical(self.packet))
        self.binding = dict(service_id="new-runtime", manifest_revision="1", worker_id="mac",
                            worker_incarnation="mac-inc", boot_epoch="gateway-boot", grant_token="fixture-grant")
        self.worker = {"worker_id": "pi", "worker_incarnation": "pi-inc"}
        self.requests = []

    def transport(self, mutate=None, fail_after=None):
        owner = self
        class Transport:
            async def request(self, path, body):
                owner.requests.append((path, copy.deepcopy(body)))
                if fail_after is not None and len(owner.requests) > fail_after:
                    raise RuntimeError("fixture disconnected")
                owner.assertEqual("/api/service-checkpoints/chunk-get", path)
                owner.assertEqual(owner.binding, body["binding"])
                owner.assertEqual(owner.worker, body["worker"])
                key = body["chunk"]["digest"]
                raw = owner.chunks.get(key)
                reply = {"type": "checkpoint_result", "id": "fixture", "status": 200,
                         "worker_id": "pi", "worker_incarnation": "pi-inc",
                         "boot_epoch": "gateway-boot", "binding": copy.deepcopy(owner.binding),
                         "checkpoint": None,
                         "result": {"digest": key, "bytes": len(raw),
                                    "data": base64.b64encode(raw).decode()}}
                if mutate is not None:
                    mutate(reply)
                return reply
        return Transport()

    async def pull(self, chunks=None, **kwargs):
        return await transfer.pull_packet(self.manifest["digest"], self.node,
            chunks if chunks is not None else Chunks(), self.transport(**kwargs),
            "new-runtime", self.worker, self.binding)

    def test_roundtrip_preserves_original_rows_and_every_sidecar_column(self):
        loaded = transfer.read_packet(self.packet, self.chunks.get, self.node)
        self.assertEqual(migration.load_source(self.backup), loaded)
        self.assertEqual(self.value, loaded["records"][0][2])
        self.assertEqual("pause", loaded["records"][1][2]["desired"])
        self.assertEqual("fixture-private-key", loaded["event_delivery"]["subscriptions"][0]["key"])

    def test_packing_is_bounded_and_does_not_create_full_json_snapshot(self):
        payload = self.packet["payload"]
        self.assertGreater(len(payload["chunks"]), 1)
        self.assertTrue(all(0 < part["bytes"] <= transfer.BLOCK_BYTES for part in payload["chunks"]))
        self.assertLess(payload["compressed_bytes"], payload["decoded_bytes"])
        self.assertFalse((self.root / "snapshot.json").exists())
        self.assertNotIn("records", payload)
        self.assertEqual(2, payload["source"]["records"])

    def test_source_fingerprint_and_packet_signature_are_required(self):
        with self.assertRaises(migration.MigrationError):
            transfer.verify_packet(self.packet, "f" * 64)
        changed = copy.deepcopy(self.packet)
        changed["payload"]["source"]["records"] += 1
        with self.assertRaises(Exception):
            transfer.verify_packet(changed, self.node)

    def test_missing_or_changed_block_never_returns_source_rows(self):
        key = self.packet["payload"]["chunks"][0]["digest"]
        raw = self.chunks.data.pop(key)
        with self.assertRaises(FileNotFoundError):
            transfer.read_packet(self.packet, self.chunks.get)
        self.chunks.data[key] = raw[:-1] + bytes([raw[-1] ^ 1])
        with self.assertRaises(ValueError):
            transfer.read_packet(self.packet, self.chunks.get)

    def test_changed_backup_is_rejected_before_export(self):
        with (self.root / "copied.sqlite").open("ab") as stream:
            stream.write(b"changed")
        with self.assertRaises(migration.MigrationError):
            transfer.pack_backup(self.backup, self.home, Chunks())

    def test_signed_source_inventory_is_checked_against_recovered_rows(self):
        changed = copy.deepcopy(self.packet)
        changed["payload"]["source"]["records"] = 999
        changed["signature"] = transfer.identity.sign(str(self.key), changed["payload"])
        with self.assertRaises(migration.MigrationError):
            transfer.read_packet(changed, self.chunks.get)

    def test_signed_decoded_length_limits_decompression(self):
        changed = copy.deepcopy(self.packet)
        changed["payload"]["decoded_bytes"] = 1
        changed["signature"] = transfer.identity.sign(str(self.key), changed["payload"])
        with self.assertRaises(migration.MigrationError):
            transfer.read_packet(changed, self.chunks.get)

    def test_planner_loader_accepts_signed_source_manifest(self):
        directory = self.root / "copied.chunks"
        directory.mkdir()
        path = self.root / "source.json"
        transfer.write_manifest(path, self.packet, directory)
        fake = types.SimpleNamespace(ChunkStore=lambda unused: self.chunks)
        with patch.dict(sys.modules, {"biomerte.checkpoint_chunks": fake}):
            self.assertEqual(migration.load_source(self.backup), migration.load_source(path))
        self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_manifest_cannot_resolve_a_different_private_directory(self):
        with self.assertRaises(migration.MigrationError):
            transfer.write_manifest(self.root / "source.json", self.packet,
                                    self.root / "elsewhere" / "chunks")
        self.assertFalse((self.root / "source.json").exists())

    async def test_private_pull_verifies_source_without_runtime_install(self):
        copied = Chunks()
        packet, receipt = await self.pull(copied)
        self.assertEqual(self.packet, packet)
        self.assertEqual(len(self.packet["payload"]["chunks"]), receipt["fetched_blocks"])
        self.assertFalse(receipt["runtime_checkpoint_installed"])
        self.assertFalse(receipt["effect_admission_changed"])
        self.assertEqual(self.packet["payload"]["source"]["digest"], receipt["source_digest"])
        self.assertTrue(all(path.endswith("/chunk-get") for path, unused in self.requests))

    async def test_partial_copy_resumes_only_missing_verified_blocks(self):
        copied = Chunks()
        with self.assertRaises(RuntimeError):
            await self.pull(copied, fail_after=2)
        self.assertEqual(1, len(copied.data))
        self.requests.clear()
        packet, receipt = await self.pull(copied)
        self.assertEqual(1, receipt["reused_blocks"])
        self.assertEqual(len(packet["payload"]["chunks"]) - 1, receipt["fetched_blocks"])
        self.assertEqual(1 + receipt["fetched_blocks"], len(self.requests))

    async def test_stale_grant_wrong_incarnation_or_epoch_never_copies(self):
        for field, value in (("worker_incarnation", "wrong"), ("boot_epoch", "wrong"),
                             ("binding", dict(self.binding, grant_token="stale")), ("status", 409)):
            with self.subTest(field=field):
                copied = Chunks()
                with self.assertRaises(migration.MigrationError):
                    await self.pull(copied, mutate=lambda reply, field=field, value=value:
                                    reply.update({field: value}))
                self.assertEqual({}, copied.data)

    async def test_remote_manifest_hash_and_encoded_bytes_are_checked(self):
        for mutate in (
                lambda reply: reply["result"].update(digest="f" * 64),
                lambda reply: reply["result"].update(data="not-base64!"),
                lambda reply: reply["result"].update(bytes=1)):
            with self.subTest(mutate=mutate):
                with self.assertRaises(migration.MigrationError):
                    await self.pull(mutate=mutate)


if __name__ == "__main__":
    unittest.main()
