import importlib.util
import json
import pathlib
import sqlite3
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "controller_source_deployment", ROOT / "deploy/controller-authority/deploy.py")
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)
migration = deploy.migration
PI, MAC, AUTH = "a" * 64, "b" * 64, "c" * 64


class SourceFenceInstallation(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = pathlib.Path(self.directory.name)
        (self.home / "node.json").write_text(json.dumps({"node": PI, "site": "site"}))
        self.database = self.home / "site.sqlite"
        self.db = sqlite3.connect(str(self.database), isolation_level=None)
        self.addCleanup(self.db.close)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE records(kind TEXT,id TEXT,value TEXT,PRIMARY KEY(kind,id))")
        self.db.execute("INSERT INTO records VALUES('work','task','{}')")
        snapshot = migration.read_snapshot(self.database, PI, "site")
        self.marker = migration.freeze_marker(snapshot, {
            "service_id": "controller-runtime", "authority_id": AUTH,
            "site": "site", "controller_aliases": [PI, MAC]}, "migration")

    def test_marker_install_is_private_exact_and_idempotent_without_database_changes(self):
        before = migration.database_inventory(self.database, PI, "site")
        result = deploy.install_source_marker(self.home, self.marker)
        path = self.home / "controller-authority.json"
        self.assertTrue(result["created"])
        self.assertFalse(result["runtime_admission_verified"])
        self.assertEqual(self.marker, json.loads(path.read_text()))
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertFalse(deploy.install_source_marker(self.home, self.marker)["created"])
        self.assertEqual(before, migration.database_inventory(self.database, PI, "site"))

    def test_changed_source_is_not_fenced_with_a_stale_backup_marker(self):
        self.db.execute("INSERT INTO records VALUES('work','later','{}')")
        with self.assertRaises(migration.MigrationError):
            deploy.install_source_marker(self.home, self.marker)
        self.assertFalse((self.home / "controller-authority.json").exists())

    def test_wrong_native_identity_and_existing_marker_are_not_replaced(self):
        with self.assertRaises(migration.MigrationError):
            deploy.install_source_marker(self.home, dict(self.marker, source_node=MAC))
        path = self.home / "controller-authority.json"
        path.write_text(json.dumps({"existing": True}))
        with self.assertRaises(migration.MigrationError):
            deploy.install_source_marker(self.home, self.marker)
        self.assertEqual({"existing": True}, json.loads(path.read_text()))


    def test_replica_verification_does_not_create_a_missing_checkpoint(self):
        path = self.home / "missing-checkpoint.sqlite"
        with self.assertRaises(migration.MigrationError):
            deploy.verify_replica({"authority_kind": "controller", "checkpoint_db": str(path)}, {})
        self.assertFalse(path.exists())

    def test_atomic_create_never_follows_or_replaces_a_marker_symlink(self):
        prior = self.home / "prior"
        prior.write_text("private prior data")
        path = self.home / "controller-authority.json"
        path.symlink_to(prior)
        with self.assertRaises(migration.MigrationError):
            deploy.install_source_marker(self.home, self.marker)
        self.assertEqual("private prior data", prior.read_text())


class ReplicaCheckpointVerification(unittest.TestCase):
    def setUp(self):
        try:
            from biomerte.service_checkpoint import CheckpointStore, checkpoint_ref
            from biomerte.service_daemon import scope_digest
            from biomerte import checkpoint_chunks
        except ImportError as error:
            raise unittest.SkipTest("Combine the controller chunk runtime before replica verification tests") from error
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = pathlib.Path(self.directory.name)
        key = self.root / "checkpoint.key"
        key.write_text("k" * 48)
        self.config = {
            "authority_kind": "controller", "service_id": "controller-runtime",
            "authority_id": AUTH, "site": "site", "controller_aliases": [PI, MAC],
            "resources": [], "providers": [], "contracts": [], "operator_actors": [],
            "operator_actor_aliases": {}, "checkpoint_key_file": str(key),
            "checkpoint_db": str(self.root / "checkpoint.sqlite")}
        store = CheckpointStore(self.config["checkpoint_db"], self.config["service_id"],
                                scope_digest(self.config), ("k" * 48).encode())
        try:
            state = migration.chunk_state({"authority": AUTH, "site": "site", "records": [
                ["assistant_responsibilities", "worker", {"desired": "pause"}],
                ["work", "task", {"id": "task"}]]}, store.chunks)
            self.packet = store.bootstrap(state, {"source_receipt": "disposable-test-fixture"})
            self.expected = checkpoint_ref(self.packet)
        finally:
            store.close()

    def test_real_root_and_shards_are_verified_without_mutation(self):
        result = deploy.verify_replica(self.config, self.expected)
        self.assertTrue(result["verified"])
        self.assertEqual(2, result["records"])
        self.assertEqual(1, result["kinds"]["assistant_responsibilities"])
        self.assertFalse(result["application_effects_executed"])
        self.assertEqual(result, deploy.verify_replica(self.config, self.packet))

    def test_stale_expected_checkpoint_is_not_accepted_as_current(self):
        expected = dict(self.expected, digest="f" * 64)
        with self.assertRaises(migration.MigrationError):
            deploy.verify_replica(self.config, expected)

    def test_tampered_root_payload_is_rejected(self):
        self.packet["payload"]["state"]["site"] = "tampered"
        db = sqlite3.connect(self.config["checkpoint_db"])
        try:
            db.execute("UPDATE packets SET packet=?", (json.dumps(self.packet),))
            db.commit()
        finally:
            db.close()
        with self.assertRaises(ValueError):
            deploy.verify_replica(self.config, self.expected)


if __name__ == "__main__":
    unittest.main()
