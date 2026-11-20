"""Migration qualification uses disposable SQLite databases, never live homes."""
import base64
import copy
import hashlib
import importlib.util
import json
import os
import pathlib
import sqlite3
import tempfile
import unittest

PATH = pathlib.Path(__file__).resolve().parents[1] / "deploy/controller-authority/migrate.py"
SPEC = importlib.util.spec_from_file_location("controller_state_migration", PATH)
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)
PI, MAC, AUTH = "a" * 64, "b" * 64, "c" * 64
TARGET = {"service_id": "controller-runtime", "authority_id": AUTH,
          "site": "same-site", "controller_aliases": [PI, MAC]}


def snapshot(node, rows):
    return {"format": migration.FORMAT, "source_node": node, "site": "same-site",
            "records": sorted(copy.deepcopy(rows), key=lambda row: (row[0], row[1]))}


def records(exported):
    return {(kind, key): value for kind, key, value in exported["state"]["records"]}


class ControllerMigration(unittest.TestCase):
    def setUp(self):
        self.pi = snapshot(PI, [
            ["_meta", "events", 5], ["_meta", "initialized", 1],
            ["events", "%020d" % 5, {"id": "pi-event", "seq": 5, "topic": "user.pi", "payload": {}}],
            ["work", "pi-work", {"id": "pi-work", "state": "open"}],
            ["assistant_responsibilities", "worker", {"id": "worker", "desired": "pause",
                                                     "state": "pause", "active_cycle": None}]])
        self.mac = snapshot(MAC, [
            ["_meta", "events", 2], ["_meta", "initialized", 1],
            ["events", "%020d" % 2, {"id": "mac-event", "seq": 2, "topic": "user.mac", "payload": {}}],
            ["work", "mac-work", {"id": "mac-work", "state": "open"}]])
        self.sources = [self.pi, self.mac]
        self.policy = {"retire_event_sources": [MAC]}

    def put(self, source, kind, key, value):
        source["records"] = [row for row in source["records"] if row[:2] != [kind, key]]
        source["records"].append([kind, key, copy.deepcopy(value)])
        source["records"].sort(key=lambda row: (row[0], row[1]))

    def plan(self, policy=None):
        return migration.make_plan(self.sources, PI, TARGET, "test-migration",
                                   self.policy if policy is None else policy)

    def export(self, plan=None):
        plan = plan or self.plan()
        markers = [migration.freeze_marker(s, TARGET, "test-migration") for s in self.sources]
        return migration.export_state(self.sources, plan, markers)

    def test_full_union_preserves_paused_responsibility_and_exact_sources(self):
        before = copy.deepcopy(self.sources)
        plan = self.plan()
        self.assertTrue(plan["ready"], plan["blocked"])
        result = records(self.export(plan))
        self.assertEqual("pause", result[("assistant_responsibilities", "worker")]["desired"])
        self.assertIn(("work", "pi-work"), result)
        self.assertIn(("work", "mac-work"), result)
        self.assertEqual(before, self.sources)
        for source in self.sources:
            for kind, key, value in source["records"]:
                retained = result.get((kind, key)) == value or any(
                    archive.get("source_controller") == source["source_node"]
                    and archive.get("kind") == kind and archive.get("key") == key
                    and archive.get("record") == value
                    for (row_kind, unused), archive in result.items()
                    if row_kind == migration.ARCHIVE)
                self.assertTrue(retained, (source["source_node"], kind, key))

    def test_secondary_event_domain_is_archived_without_replaying_it(self):
        result = records(self.export())
        self.assertEqual(5, result[("_meta", "events")])
        self.assertNotIn(("events", "%020d" % 2), result)
        archived = [value for (kind, unused), value in result.items() if kind == migration.ARCHIVE]
        self.assertTrue(any(value["record"].get("id") == "mac-event"
                            for value in archived if isinstance(value["record"], dict)))

    def test_secondary_retention_cursor_is_not_applied_to_primary_events(self):
        self.put(self.pi, "_meta", "operational_retention", {"event_floor": 3})
        self.put(self.mac, "_meta", "operational_retention", {"event_floor": 100})
        self.assertEqual({"event_floor": 3},
                         records(self.export())[("_meta", "operational_retention")])

    def test_unfinished_source_coordination_blocks_adoption(self):
        for kind, value in (
                ("_session_scope", {"session": "pending", "kind": "work", "key": "pi-work"}),
                ("_ownership_pending", {"phase": "adoption_pending"}),
                ("_coordination", {"state": "open"}),
                ("_receipt", {"signature": "x", "external_pending": True})):
            with self.subTest(kind=kind):
                self.put(self.pi, kind, "active", value)
                plan = self.plan()
                self.assertFalse(plan["ready"])
                self.assertTrue(any(item["kind"] == kind for item in plan["blocked"]))
                self.pi["records"] = [row for row in self.pi["records"] if row[:2] != [kind, "active"]]

    def test_settled_ownership_history_is_preserved(self):
        current = {"owner": PI, "local": PI, "epoch": 7, "aliases": ["worker"]}
        self.put(self.pi, "_ownership_current", "responsibility", current)
        self.assertEqual(current, records(self.export())[("_ownership_current", "responsibility")])

    def test_event_domain_with_subscription_blocks_retirement(self):
        self.put(self.mac, "subscriptions", "pending", {"id": "pending", "cursor": 1})
        plan = self.plan()
        self.assertFalse(plan["ready"])
        self.assertTrue(any(item["kind"] == "subscriptions" for item in plan["blocked"]))
        with self.assertRaises(migration.MigrationError):
            self.export(plan)

    def test_event_domain_with_nonterminal_run_or_assistant_is_not_discarded(self):
        for kind, value in (
                ("runs", {"state": "running"}),
                ("waits", {"state": "open"}),
                ("assistant_responsibilities", {"state": "pause"})):
            with self.subTest(kind=kind):
                self.put(self.mac, kind, "pending", value)
                self.assertFalse(self.plan()["ready"])
                self.mac["records"] = [row for row in self.mac["records"] if row[:2] != [kind, "pending"]]

    def test_mutable_collision_requires_exact_reviewed_source(self):
        self.put(self.pi, "actions", "same-id", {"state": "uncertain", "provider": "pi"})
        self.put(self.mac, "actions", "same-id", {"state": "succeeded", "provider": "mac"})
        plan = self.plan()
        self.assertFalse(plan["ready"])
        policy = dict(self.policy, resolutions=[{"kind": "actions", "id": "same-id", "source": PI}])
        selected = self.plan(policy)
        self.assertTrue(selected["ready"])
        value = records(self.export(selected))
        self.assertEqual("uncertain", value[("actions", "same-id")]["state"])
        self.assertTrue(any(row["kind"] == "actions" and row["source_controller"] == MAC
                            for (kind, unused), row in value.items() if kind == migration.ARCHIVE))

    def test_unused_resolution_is_rejected(self):
        with self.assertRaises(migration.MigrationError):
            self.plan(dict(self.policy, resolutions=[{"kind": "work", "id": "absent", "source": PI}]))

    def test_equivalent_receipts_coalesce_without_claiming_another_effect(self):
        receipt = {"format": 2, "signature": "same", "operation": "work.create",
                   "result": {"id": "same-work"}, "created_at": 1}
        self.put(self.pi, "_receipt", "receipt", receipt)
        self.put(self.mac, "_receipt", "receipt", dict(receipt, created_at=2))
        result = records(self.export())
        self.assertEqual(receipt["result"], result[("_receipt", "receipt")]["result"])
        self.assertNotIn((migration.RECEIPT_CONFLICTS, "receipt"), result)

    def test_ambiguous_receipt_keeps_both_originals_and_permanent_no_replay_marker(self):
        self.put(self.pi, "_receipt", "collision", {
            "format": 2, "signature": "first", "result": {"action": "pi-action"}})
        self.put(self.mac, "_receipt", "collision", {
            "format": 2, "signature": "second", "result": {"action": "mac-action"}})
        result = records(self.export())
        self.assertTrue(result[("_receipt", "collision")]["result_expired"])
        self.assertNotIn("result", result[("_receipt", "collision")])
        self.assertFalse(result[(migration.RECEIPT_CONFLICTS, "collision")]["replay_allowed"])
        originals = [row for (kind, unused), row in result.items()
                     if kind == migration.ARCHIVE and row["kind"] == "_receipt"]
        self.assertEqual({PI, MAC}, {row["source_controller"] for row in originals})
        self.assertEqual({"pi-action", "mac-action"}, {row["record"]["result"]["action"] for row in originals})

    def test_ordered_receipts_keep_maximum_floor(self):
        self.put(self.pi, "_receipt_stream", "slot", {"sequence": 7, "signature": "old"})
        self.put(self.mac, "_receipt_stream", "slot", {"sequence": 9, "signature": "new", "result": {"id": 9}})
        result = records(self.export())
        self.assertEqual(9, result[("_receipt_stream", "slot")]["sequence"])
        self.assertEqual({"id": 9}, result[("_receipt_stream", "slot")]["result"])

    def test_equal_sequence_divergence_is_quarantined(self):
        self.put(self.pi, "_receipt_stream", "slot", {"sequence": 9, "signature": "one", "result": 1})
        self.put(self.mac, "_receipt_stream", "slot", {"sequence": 9, "signature": "two", "result": 2})
        result = records(self.export())
        self.assertNotIn("result", result[("_receipt_stream", "slot")])
        self.assertEqual(9, result[(migration.STREAM_CONFLICTS, "slot")]["floor"])
        self.assertFalse(result[(migration.STREAM_CONFLICTS, "slot")]["replay_allowed"])

    def test_writer_and_physical_generation_floors_never_decrease(self):
        for kind in ("_writers", "_fence_generations"):
            self.put(self.pi, kind, "shared", 9)
            self.put(self.mac, kind, "shared", 12)
        result = records(self.export())
        self.assertEqual(12, result[("_writers", "shared")])
        self.assertEqual(12, result[("_fence_generations", "shared")])

    def test_revision_dag_heads_are_reconstructed_without_inventing_revisions(self):
        first = {"key": "document", "parents": [], "body": "first"}
        second = {"key": "document", "parents": ["first"], "body": "second"}
        self.put(self.pi, "revisions", "first", first)
        self.put(self.pi, "documents", "document", {"id": "document", "heads": ["first"], "conflict": False})
        self.put(self.mac, "revisions", "first", first)
        self.put(self.mac, "revisions", "second", second)
        self.put(self.mac, "documents", "document", {"id": "document", "heads": ["second"], "conflict": False})
        result = records(self.export())
        self.assertEqual(["second"], result[("documents", "document")]["heads"])
        self.assertEqual(first, result[("revisions", "first")])
        self.assertEqual(second, result[("revisions", "second")])

    def test_missing_document_revision_blocks_export(self):
        self.put(self.pi, "documents", "missing", {"id": "missing", "heads": ["not-present"]})
        self.assertFalse(self.plan()["ready"])

    def test_identical_artifact_bytes_keep_durable_upload(self):
        raw = b"durable source bytes"
        key = hashlib.sha256(raw).hexdigest()
        value = {"id": key, "data": base64.b64encode(raw).decode()}
        self.put(self.pi, "artifacts", key, dict(value, expires_at=4))
        self.put(self.mac, "artifacts", key, value)
        self.assertEqual(value, records(self.export())[("artifacts", key)])

    def test_artifact_hash_mismatch_is_rejected(self):
        key = "f" * 64
        self.put(self.pi, "artifacts", key, {"id": key, "data": "YQ==", "expires_at": 1})
        self.put(self.mac, "artifacts", key, {"id": key, "data": "Yg==", "expires_at": 2})
        with self.assertRaises(migration.MigrationError):
            self.plan()

    def test_source_change_after_plan_is_rejected(self):
        plan = self.plan()
        self.put(self.pi, "work", "late", {"id": "late"})
        with self.assertRaises(migration.MigrationError):
            self.export(plan)

    def test_missing_or_changed_freeze_marker_is_rejected(self):
        plan = self.plan()
        markers = [migration.freeze_marker(s, TARGET, "test-migration") for s in self.sources]
        with self.assertRaises(migration.MigrationError):
            migration.export_state(self.sources, plan, markers[:1])
        markers[0]["source_digest"] = "wrong"
        with self.assertRaises(migration.MigrationError):
            migration.export_state(self.sources, plan, markers)

    def test_physical_controller_identity_cannot_be_cloned(self):
        target = dict(TARGET, authority_id=PI)
        with self.assertRaises(migration.MigrationError):
            migration.make_plan(self.sources, PI, target, "test", self.policy)

    def test_duplicate_source_rows_are_rejected(self):
        self.pi["records"].append(copy.deepcopy(self.pi["records"][0]))
        with self.assertRaises(migration.MigrationError):
            self.plan()

    def test_inventory_digest_matches_canonical_order(self):
        summary = migration.inventory(self.pi)
        self.assertEqual(migration.digest(self.pi["records"]), summary["digest"])
        self.assertEqual(migration.encoded_size(self.pi["records"]), summary["canonical_bytes"])


class ChunkedMigration(unittest.TestCase):
    class Chunks:
        def __init__(self):
            self.raw = {}
        def write_shard(self, kind, prefix, raw):
            key = hashlib.sha256(raw).hexdigest()
            self.raw[(kind, prefix)] = raw
            return {"kind": kind, "prefix": prefix, "digest": key, "bytes": len(raw),
                    "blocks": [{"digest": key, "bytes": len(raw)}]}

    def test_chunk_manifest_preserves_all_rows_in_hash_selected_shards(self):
        rows = sorted([["work", "a", {"title": "A"}], ["work", "b", {"title": "B"}],
                       ["assistant_responsibilities", "worker", {"desired": "pause"}],
                       ["artifacts", "large", {"data": "x" * 300000}]],
                      key=lambda row: (row[0], row[1]))
        chunks = self.Chunks()
        manifest = migration.chunk_state({"authority": AUTH, "site": "same-site",
                                          "records": rows}, chunks)
        self.assertEqual("biomerte-controller-state/1", manifest["format"])
        self.assertNotIn("records", manifest)
        self.assertLess(migration.encoded_size(manifest), 4096)
        recovered = []
        for shard in manifest["shards"]:
            raw = chunks.raw[(shard["kind"], shard["prefix"])]
            self.assertEqual(hashlib.sha256(raw).hexdigest(), shard["digest"])
            for row in json.loads(raw):
                self.assertEqual(shard["kind"], row[0])
                self.assertEqual(shard["prefix"], hashlib.sha256(row[1].encode()).hexdigest()[:2])
                recovered.append(row)
        self.assertEqual(rows, sorted(recovered, key=lambda row: (row[0], row[1])))

    def test_duplicate_or_unsorted_canonical_rows_are_not_repackaged(self):
        for rows in ([["work", "z", {}], ["work", "a", {}]],
                     [["work", "a", {}], ["work", "a", {}]]):
            with self.subTest(rows=rows):
                with self.assertRaises(migration.MigrationError):
                    migration.chunk_state({"authority": AUTH, "site": "same-site",
                                           "records": rows}, self.Chunks())

    def test_bootstrap_rejects_wrong_target_before_touching_checkpoint_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "must-not-exist.sqlite"
            with self.assertRaises(migration.MigrationError):
                migration.bootstrap_controller(
                    {"state": {"authority": AUTH, "site": "same-site", "records": []}},
                    {"authority_kind": "controller", "authority_id": PI,
                     "site": "same-site", "checkpoint_db": str(path)})
            self.assertFalse(path.exists())


class PrivateSQLiteOperations(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = pathlib.Path(self.directory.name)
        self.path = self.root / "source.sqlite"
        self.connection = sqlite3.connect(str(self.path), isolation_level=None)
        self.addCleanup(self.connection.close)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA wal_autocheckpoint=0")
        self.connection.execute("CREATE TABLE records(kind TEXT,id TEXT,value TEXT,PRIMARY KEY(kind,id))")
        self.connection.execute("INSERT INTO records VALUES(?,?,?)",
                                ("work", "durable", json.dumps({"id": "durable", "title": "in WAL"})))

    def test_backup_includes_wal_and_does_not_modify_source(self):
        before = migration.database_inventory(self.path, PI, "same-site")
        target = self.root / "backup.sqlite"
        result = migration.backup(self.path, target, PI, "same-site")
        after = migration.database_inventory(self.path, PI, "same-site")
        self.assertEqual(before, after)
        self.assertEqual(before["digest"], result["digest"])
        self.assertEqual(0o600, target.stat().st_mode & 0o777)
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), result["backup_sha256"])
        self.assertEqual("in WAL", migration.read_snapshot(target, PI, "same-site")["records"][0][2]["title"])

    def test_existing_backup_and_private_output_are_not_overwritten(self):
        target = self.root / "backup.sqlite"
        migration.backup(self.path, target, PI, "same-site")
        with self.assertRaises(FileExistsError):
            migration.backup(self.path, target, PI, "same-site")
        output = self.root / "report.json"
        migration.private_json(output, {"first": True})
        with self.assertRaises(FileExistsError):
            migration.private_json(output, {"first": False})
        self.assertEqual({"first": True}, json.loads(output.read_text()))

    def test_streamed_snapshot_and_inventory_match_read_only_source(self):
        target = self.root / "snapshot.json"
        summary = migration.snapshot_file(self.path, PI, "same-site", target)
        source = migration.read_snapshot(self.path, PI, "same-site")
        self.assertEqual(source, json.loads(target.read_text()))
        self.assertEqual(migration.inventory(source), summary)
        self.assertEqual(summary, migration.database_inventory(self.path, PI, "same-site"))
        self.assertEqual(0o600, target.stat().st_mode & 0o777)


    def test_copied_backup_manifest_loads_without_whole_snapshot_json(self):
        target = self.root / "backup.sqlite"
        manifest = migration.backup(self.path, target, PI, "same-site")
        path = self.root / "backup.json"
        migration.private_json(path, manifest)
        self.assertEqual(migration.read_snapshot(self.path, PI, "same-site"),
                         migration.load_source(path))
        manifest["backup_sha256"] = "wrong"
        other = self.root / "wrong.json"
        migration.private_json(other, manifest)
        with self.assertRaises(migration.MigrationError):
            migration.load_source(other)

    def test_missing_source_does_not_create_empty_database(self):
        path = self.root / "missing.sqlite"
        with self.assertRaises(migration.MigrationError):
            migration.database_inventory(path, PI, "same-site")
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
