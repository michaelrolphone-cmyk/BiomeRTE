"""Initial scoped migration must preserve native custody and reject missing proof."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

PATH = Path(__file__).resolve().parents[1] / "deploy/service-authority/migrate.py"
SPEC = importlib.util.spec_from_file_location("scoped_migration", PATH)
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)
SOURCE, PROVIDER, CONSUMER, AUTHORITY = ("a" * 64, "b" * 64, "c" * 64, "d" * 64)
RESOURCE, CONTRACT, SITE = "relay.test", "device.outputs.stream:1", "e" * 32
SCOPE = {"service_id": "relay-authority", "authority_id": AUTHORITY, "site": SITE,
         "resources": [RESOURCE], "providers": [PROVIDER], "contracts": [CONTRACT]}


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "site.sqlite")
        self.db = sqlite3.connect(self.path)
        self.db.executescript("CREATE TABLE records(kind TEXT,id TEXT,value TEXT,PRIMARY KEY(kind,id));"
                             "CREATE TABLE position(id INTEGER PRIMARY KEY,seq INTEGER,hash TEXT);"
                             "INSERT INTO position VALUES(1,0,'');")
        self.offer_id = migration.digest([PROVIDER, CONTRACT, RESOURCE])
        self.peer = {"id": PROVIDER, "key": "public fixture", "label": "relay",
                     "scopes": ["cap.offer", "provider.poll", "provider.result",
                                "resource:" + RESOURCE, "offer:" + CONTRACT], "enabled": True}
        self.consumer = {"id": CONSUMER, "key": "consumer public fixture",
                         "label": "shared Rover", "scopes": ["cap.acquire", "stream.open"], "enabled": True}
        self.offer = {"id": self.offer_id, "provider": PROVIDER, "contract": CONTRACT,
                      "resource": RESOURCE, "state": "ready", "service": "outputs",
                      "configuration": {"lease_class": "persistent"}}
        self.put("peers", PROVIDER, self.peer)
        self.put("peers", CONSUMER, self.consumer)
        self.put("peers", "unrelated", {"id": "unrelated", "enabled": True})
        self.put("offers", self.offer_id, self.offer)
        self.put("contracts", CONTRACT, {"id": CONTRACT, "inputs": {}, "outputs": {}})
        self.put("guards", RESOURCE, {"resource": RESOURCE, "generation": 9, "holder": "root"})
        self.put("_fence_generations", RESOURCE, 8)
        self.put("_capacity", RESOURCE, 1)
        self.put("_enforcers", RESOURCE, PROVIDER)
        common = {"contract": CONTRACT, "resource": RESOURCE, "requested_resource": RESOURCE,
                  "provider": PROVIDER, "owner": CONSUMER, "generation": 9, "guard": RESOURCE}
        self.put("leases", "root", dict(common, id="root", state="bound", task="old-stream"))
        self.put("leases", "queued", dict(common, id="queued", state="queued",
                                         queue_key="001", task="old-request"))
        self.put("_lease_queue", "001", "queued")
        self.put("_stream_grants", "session", {"id": "session", "lease": "root",
            "resource": RESOURCE, "contract": CONTRACT, "provider": PROVIDER,
            "server": CONSUMER, "state": "open", "generation": 9, "expires_at": 9999999999})
        self.db.commit()
        self.plan = self.read(lambda s: migration.make_plan(s, SCOPE, [CONSUMER], "fixture"))

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def put(self, kind, key, value):
        self.db.execute("INSERT OR REPLACE INTO records VALUES(?,?,?)",
                        (kind, key, migration.canonical(value)))

    def get(self, kind, key):
        row = self.db.execute("SELECT value FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
        return json.loads(row[0]) if row else None

    def read(self, fn, limits=None):
        self.db.commit()
        source = migration.Source(self.path, SOURCE, SITE, limits)
        try:
            return fn(source)
        finally:
            source.close()

    def receipt(self, command, result):
        value = {"format": 2, "operation": command["operation"],
                 "signature": migration.digest([command["operation"], command["arguments"]]),
                 "result": copy.deepcopy(result), "created_at": 123.0}
        self.put("_receipt", migration.digest([command["actor"], command["request_id"]]), value)

    def cutoff(self):
        for command in self.plan["cutoff_commands"]:
            if command["operation"] == "peer.revoke":
                value = dict(self.peer, enabled=False)
                self.put("peers", PROVIDER, value)
            else:
                value = dict(self.offer, state="offline")
                self.put("offers", self.offer_id, value)
                value = dict(value, capacity=1)
            self.receipt(command, value)
        self.db.commit()

    def drain(self):
        self.cutoff()
        for command in self.plan["drain_commands"]:
            args = command["arguments"]
            if command["operation"] == "cap.release":
                value = self.get("leases", args["lease"])
                value.update(state="released", released_at=124.0, expires_at=424.0)
                if value.pop("queue_key", None):
                    self.db.execute("DELETE FROM records WHERE kind='_lease_queue' AND id='001'")
                self.put("leases", value["id"], value)
                if value["id"] == "root":
                    self.put("guards", RESOURCE, {"resource": RESOURCE, "generation": 9, "holder": ""})
                    self.put("_fence_generations", RESOURCE, 9)
            else:
                value = self.get("_stream_grants", args["session"])
                value.update(state="closed", expires_at=124.0, closed_consumers=["root"])
                self.put("_stream_grants", value["id"], value)
            self.receipt(command, value)
        self.db.commit()

    def exported(self):
        return self.read(lambda s: migration.export_state(s, SCOPE, self.plan))

    def test_plan_denies_provider_then_offers_before_drain(self):
        self.assertEqual(self.plan["cutoff_commands"][0]["operation"], "peer.revoke")
        self.assertEqual(self.plan["cutoff_commands"][1]["arguments"]["state"], "offline")
        self.assertEqual(self.plan["drain_commands"][0]["arguments"], {"lease": "queued"})
        self.assertFalse(self.plan["physical_quiescence_proven"])

    def test_export_copies_permissions_only_to_destination_and_preserves_source(self):
        self.drain()
        before = list(self.db.execute("SELECT kind,id,value FROM records ORDER BY kind,id"))
        result = self.exported()
        destination = {(kind, key): value for kind, key, value in result["state"]["records"]}
        self.assertEqual(destination[("peers", PROVIDER)], self.peer)
        self.assertEqual(destination[("offers", self.offer_id)], self.offer)
        self.assertEqual(destination[("peers", CONSUMER)], self.consumer)
        self.assertFalse(self.get("peers", PROVIDER)["enabled"])
        self.assertEqual(self.get("offers", self.offer_id)["state"], "offline")
        self.assertEqual(self.get("peers", CONSUMER), self.consumer)
        self.assertEqual(before, list(self.db.execute("SELECT kind,id,value FROM records ORDER BY kind,id")))
        self.assertNotIn(("peers", "unrelated"), destination)
        self.assertEqual(destination[("_fence_generations", RESOURCE)], 9)
        self.assertEqual(destination[("leases", "queued")]["state"], "released")
        origin = destination[("_service_source", "initial")]
        former = {(kind, key): value for kind, key, value in origin["former_native_records"]}
        self.assertEqual(former[("leases", "queued")]["state"], "queued")
        self.assertFalse(origin["replay_allowed"])
        self.assertFalse(result["authorization"]["source_receipt"]["physical_quiescence_proven"])
        self.assertEqual(result["scope_digest"], migration.scope_digest(SCOPE))

    def test_arbitrary_authorization_is_not_a_plan(self):
        with self.assertRaises(migration.MigrationError):
            self.read(lambda s: migration.export_state(s, SCOPE, {"source_receipt": "approved"}))

    def test_missing_durable_cutoff_receipt_rejected(self):
        with self.assertRaises(migration.MigrationError):
            self.exported()

    def test_provider_reenabled_after_receipt_rejected(self):
        self.drain()
        self.put("peers", PROVIDER, self.peer)
        with self.assertRaises(migration.MigrationError):
            self.exported()

    def test_offer_readvertised_after_receipt_rejected(self):
        self.drain()
        self.put("offers", self.offer_id, self.offer)
        with self.assertRaises(migration.MigrationError):
            self.exported()

    def test_receipt_must_bind_exact_operation_and_arguments(self):
        self.drain()
        command = self.plan["cutoff_commands"][0]
        key = migration.digest([command["actor"], command["request_id"]])
        value = self.get("_receipt", key)
        value["signature"] = "0" * 64
        self.put("_receipt", key, value)
        with self.assertRaises(migration.MigrationError):
            self.exported()

    def test_missing_drain_receipt_is_not_inferred_from_idle_state(self):
        self.drain()
        command = self.plan["drain_commands"][0]
        self.db.execute("DELETE FROM records WHERE kind='_receipt' AND id=?",
                        (migration.digest([command["actor"], command["request_id"]]),))
        with self.assertRaises(migration.MigrationError):
            self.exported()

    def test_queued_request_after_cutoff_blocks_export(self):
        self.drain()
        value = self.get("leases", "root")
        value.update(id="late-queue", state="queued")
        self.put("leases", "late-queue", value)
        with self.assertRaises(migration.MigrationError):
            self.exported()

    def test_uncertain_action_retained_and_never_replayed(self):
        self.drain()
        action = {"id": "uncertain", "lease": "root", "resource": RESOURCE,
                  "contract": CONTRACT, "provider": PROVIDER, "state": "uncertain"}
        self.put("actions", action["id"], action)
        with self.assertRaises(migration.MigrationError):
            self.exported()
        self.assertEqual(self.get("actions", "uncertain"), action)

    def test_new_open_stream_blocks_export(self):
        self.drain()
        value = self.get("_stream_grants", "session")
        value.update(id="late-stream", state="open")
        self.put("_stream_grants", value["id"], value)
        with self.assertRaises(migration.MigrationError):
            self.exported()

    def test_held_guard_blocks_export(self):
        self.drain()
        self.put("guards", RESOURCE, {"resource": RESOURCE, "generation": 9, "holder": "unknown"})
        with self.assertRaises(migration.MigrationError):
            self.exported()

    def test_changed_target_scope_is_rejected(self):
        self.drain()
        changed = dict(SCOPE, authority_id="f" * 64)
        with self.assertRaises(migration.MigrationError):
            self.read(lambda s: migration.export_state(s, changed, self.plan))

    def test_shared_provider_cannot_be_revoked_for_one_resource(self):
        value = copy.deepcopy(self.peer)
        value["scopes"].append("resource:camera")
        self.put("peers", PROVIDER, value)
        with self.assertRaises(migration.MigrationError):
            self.read(lambda s: migration.make_plan(s, SCOPE, [CONSUMER], "other"))

    def test_shared_consumer_change_requires_review(self):
        self.drain()
        value = dict(self.consumer, label="changed externally")
        self.put("peers", CONSUMER, value)
        with self.assertRaises(migration.MigrationError):
            self.exported()

    def test_large_receipt_history_is_not_scanned(self):
        for i in range(700):
            self.put("_receipt", "unrelated-" + str(i), {"unrelated": i})
        plan = self.read(lambda s: migration.make_plan(s, SCOPE, [CONSUMER], "other"))
        self.assertEqual(len(plan["cutoff_commands"]), 2)
        with self.assertRaises(migration.MigrationError):
            self.read(lambda s: s.scan("_receipt"))

    def test_current_state_and_bytes_are_bounded(self):
        with self.assertRaises(migration.MigrationError):
            self.read(lambda s: migration.make_plan(s, SCOPE, [CONSUMER], "other"),
                      {"rows_per_kind": 1})
        with self.assertRaises(migration.MigrationError):
            self.read(lambda s: migration.make_plan(s, SCOPE, [CONSUMER], "other"),
                      {"row_bytes": 16})

    def test_ordered_request_floor_preserved_without_unrelated_payload(self):
        key = migration.digest([CONSUMER, 0])
        self.put("_receipt_stream", key, {"sequence": 7, "signature": "sig",
                                        "result": {"resource": "camera", "private": "unrelated"}})
        self.drain()
        state = {(kind, row): value for kind, row, value in self.exported()["state"]["records"]}
        self.assertEqual(state[("_receipt_stream", key)], {"sequence": 7, "signature": "sig"})

    def test_private_output_refuses_overwrite(self):
        path = os.path.join(self.temp.name, "result.json")
        migration.write_private(path, {"value": 1})
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        with self.assertRaises(OSError):
            migration.write_private(path, {"value": 2})

    def test_pruned_released_lease_recovered_only_from_real_receipt(self):
        self.drain()
        self.db.execute("DELETE FROM records WHERE kind='leases' AND id='queued'")
        state = {(kind, row): value for kind, row, value in self.exported()["state"]["records"]}
        self.assertEqual(state[("leases", "queued")]["state"], "released")

    def test_rehashed_plan_cannot_omit_provider_cutoff(self):
        self.drain()
        changed = copy.deepcopy(self.plan)
        changed["cutoff_commands"] = changed["cutoff_commands"][1:]
        changed.pop("plan_digest")
        changed["plan_digest"] = migration.digest(changed)
        with self.assertRaises(migration.MigrationError):
            self.read(lambda s: migration.export_state(s, SCOPE, changed))

    def test_rehashed_plan_cannot_omit_queued_drain(self):
        self.drain()
        changed = copy.deepcopy(self.plan)
        changed["drain_commands"] = changed["drain_commands"][1:]
        changed.pop("plan_digest")
        changed["plan_digest"] = migration.digest(changed)
        with self.assertRaises(migration.MigrationError):
            self.read(lambda s: migration.export_state(s, SCOPE, changed))



    def test_native_orphan_submission_is_unknown_history_not_live_custody(self):
        record = {"id": "old-operation", "lease": "pruned-native-lease",
                  "contract": CONTRACT, "resource": RESOURCE, "owner": CONSUMER,
                  "inputs": {"channel": 1}}
        self.put("_submissions", "old-operation", record)
        self.put("_submission_active", "old-operation", {"id": "old-operation"})
        self.plan = self.read(lambda s: migration.make_plan(s, SCOPE, [CONSUMER], "fixture"))
        self.assertEqual(self.plan["submission_origins"][0]["lease_state"], None)
        self.assertEqual(self.plan["submission_origins"][0]["action_state"], None)
        self.assertFalse(self.plan["submission_origins"][0]["result_known"])
        self.drain()
        result = self.exported()
        destination = {(kind, key): value for kind, key, value in result["state"]["records"]}
        self.assertNotIn(("_submissions", "old-operation"), destination)
        historical = {(kind, key): value for kind, key, value in
                      destination[("_service_source", "initial")]["former_native_records"]}
        self.assertEqual(historical[("_submissions", "old-operation")], record)
        self.assertNotIn(("_submission_active", "old-operation"), destination)
        self.assertNotIn(("_submission_leases", "pruned-native-lease"), destination)
        origin = destination[("_service_source", "initial")]["submission_origins"][0]
        self.assertFalse(origin["result_known"])
        self.assertFalse(origin["replay_allowed"])
        self.assertFalse(result["physical_quiescence_proven"])

    def test_native_queued_submission_is_cancelled_by_its_existing_lease(self):
        self.put("_submissions", "queued-request", {"id": "queued-request", "lease": "queued",
                 "contract": CONTRACT, "resource": RESOURCE, "owner": CONSUMER, "inputs": {}})
        self.put("_submission_active", "queued-request", {"id": "queued-request"})
        self.put("_submission_leases", "queued", "queued-request")
        self.plan = self.read(lambda s: migration.make_plan(s, SCOPE, [CONSUMER], "fixture"))
        self.assertEqual(self.plan["drain_commands"][0]["arguments"], {"lease": "queued"})
        self.drain()
        result = self.exported()
        destination = {(kind, key): value for kind, key, value in result["state"]["records"]}
        self.assertEqual(destination[("leases", "queued")]["state"], "released")
        self.assertNotIn(("_submissions", "queued-request"), destination)
        self.assertNotIn(("_submission_active", "queued-request"), destination)
        self.assertNotIn(("_submission_leases", "queued"), destination)
        self.assertFalse(destination[("_service_source", "initial")]["replay_allowed"])

    def test_submission_with_uncertain_action_still_blocks_automatic_drain(self):
        self.put("_submissions", "unknown-effect", {"id": "unknown-effect", "lease": "root",
                 "contract": CONTRACT, "resource": RESOURCE, "owner": CONSUMER, "inputs": {}})
        self.put("actions", "unknown-effect", {"id": "unknown-effect", "lease": "root",
                 "contract": CONTRACT, "resource": RESOURCE, "state": "uncertain"})
        with self.assertRaises(migration.MigrationError):
            self.read(lambda s: migration.make_plan(s, SCOPE, [CONSUMER], "fixture"))



    def test_real_native_maintenance_cannot_recreate_historical_work(self):
        from biomerte.api import Context
        from biomerte import providers
        from biomerte.store import Store
        self.put("_submissions", "old-operation", {"id": "old-operation",
                 "lease": "pruned-native-lease", "contract": CONTRACT,
                 "resource": RESOURCE, "owner": CONSUMER, "inputs": {}})
        self.put("_submission_active", "old-operation", {"id": "old-operation"})
        self.put("_submissions", "queued-request", {"id": "queued-request",
                 "lease": "queued", "contract": CONTRACT, "resource": RESOURCE,
                 "owner": CONSUMER, "inputs": {}})
        self.put("_submission_active", "queued-request", {"id": "queued-request"})
        self.put("_submission_leases", "queued", "queued-request")
        self.plan = self.read(lambda s: migration.make_plan(s, SCOPE, [CONSUMER], "fixture"))
        self.drain()
        exported = self.exported()
        directory = os.path.join(self.temp.name, "actual-native-maintenance")
        os.makedirs(directory)
        destination = Store(directory, "site")
        def seed(tx):
            for kind, key, value in exported["state"]["records"]:
                tx.put(kind, key, value)
        destination.execute(seed)
        for unused in range(3):
            destination.execute(lambda tx: providers.settle_submissions(tx, Context(None)))
        def check(tx):
            for kind in ("actions", "leases", "_submissions",
                         "_submission_active", "_submission_leases"):
                self.assertEqual(tx.rows(kind), [], kind)
            history = tx.need("_service_source", "initial")
            original_ids = {key for kind, key, value in history["former_native_records"]
                            if kind == "_submissions"}
            self.assertEqual(original_ids, {"old-operation", "queued-request"})
            self.assertEqual(history["source_controller"], SOURCE)
            self.assertFalse(history["replay_allowed"])
        destination.execute(check, read_only=True)


if __name__ == "__main__":
    unittest.main()
