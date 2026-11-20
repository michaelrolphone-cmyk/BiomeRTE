"""Controller ownership, exact signatures, complete state and incremental snapshots."""
import asyncio
import copy
import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from biomerte.app import Application, initialize
from biomerte.api import Context, command
from biomerte import assistant_support, authorization, providers
from biomerte.controller_authority import ControllerAuthority
from biomerte.identity import generate, fingerprint, sign
from biomerte.service_checkpoint import canonical
from biomerte.store import Fault, digest

B = dict(service_id="site-controller", manifest_revision="r1", worker_id="mac",
         worker_incarnation="mac-1", boot_epoch="epoch-1", grant_token="grant-1")


class ControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory()
        cls.proxy_key = os.path.join(cls.keys.name, "proxy.key")
        cls.proxy_public = generate(cls.proxy_key)
        cls.proxy = fingerprint(cls.proxy_public)
        cls.peer_key = os.path.join(cls.keys.name, "peer.key")
        cls.peer_public = generate(cls.peer_key)
        cls.peer = fingerprint(cls.peer_public)

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.node = initialize(self.tmp.name, "controller-test")
        self.config = dict(authority_kind="controller", service_id=B["service_id"],
            authority_id=self.node["node"], site=self.node["site"],
            controller_aliases=["physical-pi", "physical-mac"],
            operator_actors=[self.proxy], operator_actor_aliases={self.proxy: "admin"})
        self.marker("managed-authority")
        self.app = Application(self.tmp.name, controller_runtime=self.config)
        self.addCleanup(self.close_app, self.app)
        self.authority = ControllerAuthority(self.app, self.config, B)
        self.app.stores["site"].execute(lambda tx: [
            tx.put("peers", self.proxy, dict(id=self.proxy, key=self.proxy_public,
                   enabled=True, scopes=["controller.operator"])),
            tx.put("peers", self.peer, dict(id=self.peer, key=self.peer_public,
                   enabled=True, scopes=["system.info","knowledge.get"]))])

    @staticmethod
    def close_app(app):
        for store in app.stores.values():
            store.close()

    def marker(self, mode):
        value = dict(format=1, mode=mode, site=self.node["site"], service_id=B["service_id"],
                     authority_id=self.node["node"], controller_aliases=["physical-pi","physical-mac"])
        if mode == "frozen-source":
            value.update(source_node=self.node["node"], migration_id="migration-1", source_digest="a"*64)
        with open(os.path.join(self.tmp.name, "controller-authority.json"), "w") as stream:
            json.dump(value, stream)
        return value

    def envelope(self, operation, arguments=None, request="request-1", controller="physical-pi", peer=False):
        value = dict(site=self.node["site"], realm="site", operation=operation,
                     arguments=arguments or {}, id=request)
        if controller is not None:
            value["controller"] = controller
        key, actor = (self.peer_key, self.peer) if peer else (self.proxy_key, self.proxy)
        return dict(value, auth=dict(actor=actor, signature=sign(key, value)))

    def call(self, operation, arguments=None, request="request-1", **kwargs):
        return self.authority.call(self.envelope(operation, arguments, request, **kwargs), False)

    def records(self):
        state = self.authority.snapshot()
        rows = []
        for shard in state["shards"]:
            raw = self.authority.snapshot_chunk(shard["digest"])
            self.assertEqual(hashlib.sha256(raw).hexdigest(), shard["digest"])
            self.assertEqual(len(raw), shard["bytes"])
            rows.extend(json.loads(raw))
        return {(kind, key): value for kind, key, value in rows}

    def test_signed_alias_preserves_original_signature(self):
        result = self.call("system.info")
        self.assertEqual(result["node"], self.node["node"])
        changed = self.envelope("system.info")
        changed["controller"] = "physical-mac"
        with self.assertRaises(Fault):
            self.authority.call(changed, False)
        with self.assertRaises(Fault):
            self.call("system.info", controller="unrelated")
        with self.assertRaises(Fault):
            self.app.call("system.info", realm="local", controller="physical-pi")

    def test_operator_mapping_replays_existing_admin_receipt_and_keeps_signer(self):
        args = dict(key="receipt-test", body={"value": 1})
        original = self.app.call("knowledge.put", args, "prior-native-admin")
        repeated = self.call("knowledge.put", args, "prior-native-admin")
        self.assertEqual(original, repeated)
        self.call("knowledge.put", dict(key="second", body=2), "proxy-new")
        receipt = self.app.stores["site"].execute(
            lambda tx: tx.need("_receipt", digest(["admin","proxy-new"])), read_only=True)
        self.assertEqual(receipt["authenticated_actor"], self.proxy)
        with self.assertRaises(Fault):
            self.call("knowledge.put", args, "unlisted", peer=True)

    def test_disabled_operator_key_is_not_elevated(self):
        def revoke(tx):
            peer = tx.need("peers", self.proxy)
            peer["enabled"] = False
            tx.put("peers", self.proxy, peer)
        self.app.stores["site"].execute(revoke)
        with self.assertRaises(Fault):
            self.call("system.info")

    def test_pending_preparation_and_external_sessions_are_not_execution_paths(self):
        with self.assertRaises(Fault) as raised:
            self.authority.call(self.envelope("system.info"), True)
        self.assertEqual(raised.exception.status, 503)
        with self.assertRaises(Fault):
            self.call("session.open", {"session":"external"})
        with self.assertRaises(Fault):
            self.authority.call(dict(self.envelope("system.info"), extra=True), False)

    def test_frozen_source_rejects_existing_app_startup_tick_and_all_write_entries(self):
        self.marker("frozen-source")
        with self.assertRaises(Fault):
            self.app.call("knowledge.put", dict(key="blocked", body=1), "blocked")
        with self.assertRaises(Fault):
            self.app.tick("site")
        with self.assertRaises(Fault):
            self.app.begin_pair(self.peer_public, "blocked")
        with self.assertRaises(Fault):
            self.app.stores["site"].execute(lambda tx: tx.put("future-kind","id",{}))
        for operation, arguments in (("provider.poll",{"provider":self.peer}),
                                      ("stream.inspect",{"session":"old-stream"}),
                                      ("bus.poll",{"name":"old-worker"})):
            with self.assertRaises(Fault) as raised:
                self.app.call(operation, arguments)
            self.assertEqual(raised.exception.status, 409)
        self.app.call("knowledge.put", dict(key="local-only", body=1), "local-only", "local")
        passive = Application(self.tmp.name)
        self.addCleanup(self.close_app, passive)
        self.assertEqual(passive.health["site"], "managed-read-only")
        self.assertEqual(passive.call("system.info")["node"], self.node["node"])
        with self.assertRaises(Fault):
            passive.call("knowledge.put", dict(key="blocked", body=1), "blocked")

    def test_plain_cli_cannot_write_managed_home(self):
        passive = Application(self.tmp.name)
        self.addCleanup(self.close_app, passive)
        with self.assertRaises(Fault):
            passive.call("knowledge.put", dict(key="bypass", body=1), "bypass")
        with self.assertRaises(Fault):
            passive.tick("site")

    def test_marker_identity_is_bound_to_source_node(self):
        value = self.marker("frozen-source")
        value["source_node"] = "different-node"
        with open(os.path.join(self.tmp.name, "controller-authority.json"), "w") as stream:
            json.dump(value, stream)
        with self.assertRaises(Fault):
            self.app.call("knowledge.put", dict(key="wrong-source", body=1), "wrong")

    def test_every_record_kind_is_checkpointed_and_idle_snapshot_does_not_read_payloads(self):
        supplied = {
            ("assistant_responsibilities","worker"): {"epoch": 7, "desired":"pause"},
            ("assistant_checkpoints","checkpoint"): {"revision": 9},
            ("runs","original-run"): {"state":"running"},
            ("waits","original-wait"): {"state":"waiting"},
            ("_receipt_stream","receipt-stream"): {"sequence":12},
            ("unknown_future_kind","new"): {"retained":"without whitelist"},
            ("artifacts","large"): {"body":"x"*120000}}
        self.app.stores["site"].execute(lambda tx: [tx.put(k,i,v) for (k,i),v in supplied.items()])
        restored = self.records()
        for key, value in supplied.items():
            self.assertEqual(restored[key], value)
        before = self.authority.snapshot_reads
        state = self.authority.snapshot()
        self.assertEqual(self.authority.snapshot_reads, before)
        self.assertEqual(state, self.authority.snapshot())

    def test_single_record_change_only_reads_its_bucket_and_removes_deleted_shard(self):
        self.app.stores["site"].execute(lambda tx: tx.put("artifacts","large",{"body":"x"*200000}))
        before = self.authority.snapshot()
        artifact = [row for row in before["shards"] if row["kind"]=="artifacts"]
        reads = self.authority.snapshot_reads
        self.app.stores["site"].execute(lambda tx: tx.put("future-kind","one",{"value":1}))
        after = self.authority.snapshot()
        self.assertEqual(self.authority.snapshot_reads-reads, 1)
        self.assertEqual([row for row in after["shards"] if row["kind"]=="artifacts"], artifact)
        reads = self.authority.snapshot_reads
        self.app.stores["site"].execute(lambda tx: tx.drop("future-kind","one"))
        after = self.authority.snapshot()
        self.assertEqual(self.authority.snapshot_reads-reads, 1)
        self.assertFalse(any(row["kind"]=="future-kind" for row in after["shards"]))

    def test_direct_sql_dirty_tracking_and_restart_preserve_full_root(self):
        before = self.authority.snapshot()
        self.app.stores["site"].execute(lambda tx: tx.db.execute(
            "INSERT INTO records VALUES(?,?,?)", ("future-kind","sql",json.dumps({"v":1}))))
        self.assertEqual(self.records()[("future-kind","sql")], {"v":1})
        before = self.authority.snapshot()
        replacement = ControllerAuthority(self.app, self.config, B)
        self.assertEqual(replacement.snapshot(), before)

    def test_logical_takeover_preserves_pending_effects_and_assistant_epoch(self):
        supplied = {
            ("actions","operation"): {"id":"operation","state":"uncertain","generation":13},
            ("leases","lease"): {"id":"lease","state":"bound","generation":13,"resource":"terminal","capacity":16},
            ("assistant_responsibilities","worker"): {"id":"worker","epoch":4,"active_cycle":"cycle"}}
        self.app.stores["site"].execute(lambda tx: [tx.put(k,i,v) for (k,i),v in supplied.items()])
        self.authority.prepare_fences("a"*64, {"terminal":999})
        evidence = self.authority.finish_fences()
        self.assertEqual(evidence["fence_kind"], "logical-controller")
        self.assertEqual(evidence["resources"], {})
        restored = self.records()
        for key, value in supplied.items():
            self.assertEqual(restored[key], value)
        self.assertEqual(evidence, self.authority.finish_fences())

    def test_conflicting_receipts_never_replay_after_normal_receipt_retention(self):
        args = dict(key="conflicting",body=1)
        key = digest(["admin","old-call"])
        self.app.stores["site"].execute(lambda tx: tx.put(
            "_controller_receipt_conflicts",key,{"replay_allowed":False}))
        with self.assertRaises(Fault) as raised:
            self.call("knowledge.put", args, "old-call")
        self.assertEqual(raised.exception.status, 409)
        self.assertIn("replay_allowed=false",str(raised.exception))
        slot_key = digest(["admin",2])
        def floor(tx):
            tx.put("_controller_receipt_stream_conflicts",slot_key,{"floor":8})
            tx.put("_receipt_stream",slot_key,{"sequence":8,"signature":"history"})
        self.app.stores["site"].execute(floor)
        with self.assertRaises(Fault):
            self.call("knowledge.put",args,{"stream":2,"sequence":8})
        self.call("knowledge.put",args,{"stream":2,"sequence":9})

    def test_maintenance_uses_full_workflow_tick(self):
        self.app.call("routine.publish", dict(name="controller-test",
            steps=[dict(type="service",operation="knowledge.put",
                        args={"key":"tick-result","body":True},save="result")],
            inputs={},outputs={},result={}), "routine")
        run = self.app.call("process.start",dict(name="controller-test",inputs={}),"start")
        for unused in range(6):
            self.authority.maintain()
        result = self.app.call("process.get",{"run":run["id"]})
        self.assertEqual(result["state"], "completed")

    def test_native_assistant_control_requires_exact_grant_and_preserves_ids(self):
        definition = dict(id="standing-worker",goal={"description":"test","criteria":["done"]},
            binding_revision="cycle-pin",cycle_definition="assistant.lifecycle",
            episode_definition="assistant.reasoning",episode_revision="episode-pin")
        def seed(tx):
            tx.put("assistant_config","current",{"operator_approved":True})
            tx.put("process_versions","cycle-pin",{"name":"assistant.lifecycle","graph":{}})
            tx.put("process_versions","episode-pin",{"name":"assistant.reasoning","graph":{}})
            action = assistant_support.cli_action(tx,{}, "assistant.register",
                                                   {"definition":definition},Context(self.app))
            self.assertEqual(action["contract"], assistant_support.CONTROLLER_CONTRACT)
            self.assertNotIn("argv", action["inputs"])
            return action
        action = self.app.stores["site"].execute(seed)
        with self.assertRaises(Fault):
            self.app.stores["site"].execute(lambda tx: providers.submit(
                tx,Context(self.app),action["contract"],action["resource"],action["inputs"],"without-grant"))
        proposal = assistant_support.proposal(action,{}, "Register exactly the reviewed worker")
        def permit_and_send(tx):
            tx.put("authorization_requests","approval",{"proposal_digest":digest(proposal)})
            tx.put("authorization_grants","grant",dict(id="grant",state="active",
                request_id="approval",proposal_digest=digest(proposal),proposal_revision=1,
                proposal=proposal,allowed_actions=[action],scope="single",
                response_id="signed-human-response",decision_id="decision"))
            return authorization.dispatch(tx,Context(self.app),"grant",proposal,"original-operation")
        result = self.app.stores["site"].execute(permit_and_send)
        self.assertEqual(result["state"], "succeeded")
        again = self.app.stores["site"].execute(lambda tx: authorization.dispatch(
            tx,Context(self.app),"grant",proposal,"original-operation"))
        self.assertEqual(again["result"], result["result"])
        row = self.app.stores["site"].execute(
            lambda tx: tx.need("actions","original-operation"),read_only=True)
        self.assertEqual(row["authorization_id"],"grant")
        self.assertEqual(row["controller_request_id"],
                         "assistant-"+digest(["assistant.register",{"definition":definition}]))
        changed = copy.deepcopy(proposal)
        changed["action"]["inputs"]["arguments"]["definition"]["goal"] = {"description":"changed"}
        with self.assertRaises(Fault):
            self.app.stores["site"].execute(lambda tx: authorization.dispatch(
                tx,Context(self.app),"grant",changed,"other-operation"))


    def test_legacy_stream_authority_keeps_its_identity_and_signing_secret(self):
        import base64
        import hashlib
        import hmac
        import time
        from biomerte import streams
        self.app.config.update(stream_router_secret="logical-secret-"+"l"*40,
            stream_gateway_url="wss://router.example/stream",
            stream_authorities={"physical-pi":{"secret":"legacy-secret-"+"p"*40,
                                               "url":"wss://router.example/stream"}})
        grant=dict(id="old-stream",authority="physical-pi",site=self.app.site,realm="site",
                   lease="old-lease",resource="camera",provider="provider",server="admin",
                   generation=8,state="open",expires_at=time.time()+600)
        def seed(tx):
            tx.put("leases","old-lease",dict(id="old-lease",guard="camera",resource="camera",
                                           owner="admin",generation=8,state="bound"))
            tx.put("guards","camera",dict(holder="old-lease",generation=8))
            tx.put("_stream_grants","old-stream",grant)
        self.app.stores["site"].execute(seed)
        observed=self.authority.inspect("old-stream","physical-pi")
        self.assertEqual(observed["authority"],"physical-pi")
        token=streams.token(Context(self.app),grant,"provider")
        encoded,signature=token.split(".")
        raw=base64.urlsafe_b64decode(encoded+"="*(-len(encoded)%4))
        expected=hmac.new(self.app.config["stream_authorities"]["physical-pi"]["secret"].encode(),
                          raw,hashlib.sha256).hexdigest()
        self.assertEqual(signature,expected)
        self.assertEqual(json.loads(raw)["authority"],"physical-pi")
        with self.assertRaises(Fault):
            self.authority.inspect("old-stream","physical-mac")

    def test_managed_status_distinguishes_controller_placement_from_shared_journal(self):
        value=self.app.call("cluster.status")
        self.assertEqual(value["mode"],"managed-controller")
        self.assertEqual(value["service_id"],B["service_id"])
        self.assertEqual(value["placement_binding"],B)
        self.marker("frozen-source")
        passive=Application(self.tmp.name)
        self.addCleanup(self.close_app,passive)
        status=passive.call("cluster.status")
        self.assertEqual(status["mode"],"managed-source")
        self.assertEqual(status["site_writes"],"frozen")


    def test_frozen_background_reports_intentional_read_only_health(self):
        from biomerte.web import background
        self.marker("frozen-source")
        class Stop:
            def __init__(self): self.done=False
            def is_set(self): return self.done
            def wait(self,seconds): self.done=True
        background(self.app,Stop(),"site")
        self.assertEqual(self.app.health["site"],"managed-read-only")
        self.assertNotIn("site",getattr(self.app,"background_errors",{}))


if __name__ == "__main__":
    unittest.main()
