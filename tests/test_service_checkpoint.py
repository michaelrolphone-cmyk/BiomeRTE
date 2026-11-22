import copy
import os
import tempfile
import unittest
from biomerte.service_checkpoint import (
    ADAPTER_PROTOCOL, CheckpointStore, CheckpointError, checkpoint_ref,
    select_current, digest)

B = dict(service_id="relay-authority",manifest_revision="r1",worker_id="pi",
         worker_incarnation="pi-1",boot_epoch="witness-1",grant_token="grant-1")


class Checkpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name,"checkpoints.sqlite")
        self.store = CheckpointStore(self.path,"relay-authority","scope-1",b"k"*32)
        self.genesis = self.store.bootstrap({"rows":[]},{"source_receipt":"scoped-source-frozen"})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def request(self, operation, **kw):
        return dict(protocol=ADAPTER_PROTOCOL,operation=operation,id=operation+"-1",
                    binding=dict(B),**kw)

    def test_packet_persists_across_restart(self):
        packet = self.store.advance({"rows":[["guards","relay",{"generation":184}]]},B)
        self.store.close()
        self.store = CheckpointStore(self.path,"relay-authority","scope-1",b"k"*32)
        self.assertEqual(self.store.packet(),packet)
        self.assertEqual(self.store.head(),checkpoint_ref(packet))

    def test_no_physical_resources_skips_checkpoint_archive(self):
        queries = []
        self.store.db.set_trace_callback(queries.append)
        try:
            self.assertEqual({}, self.store.generation_floors([]))
            self.assertEqual([], queries)
        finally:
            self.store.db.set_trace_callback(None)

    def test_signature_tampering_rejected(self):
        packet=copy.deepcopy(self.genesis)
        packet["payload"]["state"]={"rows":[["guards","relay",{"generation":1}]]}
        with self.assertRaises(CheckpointError):
            self.store.install(packet)

    def test_signed_wrong_scope_rejected(self):
        other=CheckpointStore(os.path.join(self.tmp.name,"other.sqlite"),
                              "relay-authority","other-scope",b"k"*32)
        try:
            packet=other.bootstrap({"rows":[]},{"source_receipt":"different"})
            with self.assertRaises(CheckpointError):
                self.store.install(packet)
        finally:
            other.close()

    def test_rollback_rejected(self):
        self.store.advance({"rows":[1]},B)
        with self.assertRaises(CheckpointError):
            self.store.install(self.genesis)

    def test_same_sequence_different_packet_rejected(self):
        conflicting=self.store._sign(dict(self.genesis["payload"],state={"rows":[2]}))
        with self.assertRaises(CheckpointError):
            self.store.install(conflicting)

    def test_exact_install_and_advance_retry_are_idempotent(self):
        self.assertEqual(self.store.install(self.genesis),self.store.head())
        packet=self.store.advance({"rows":[1]},B)
        self.assertEqual(packet,self.store.advance({"rows":[1]},B))

    def test_explicit_bootstrap_custody_required(self):
        empty=CheckpointStore(os.path.join(self.tmp.name,"empty.sqlite"),
                              "relay-authority","scope-1",b"k"*32)
        try:
            with self.assertRaises(CheckpointError):
                empty.bootstrap({"rows":[]},{})
        finally:
            empty.close()

    def test_preparation_requires_exact_current_head(self):
        request=self.request("prepare")
        wrong=dict(self.store.head(),digest="f"*64)
        with self.assertRaises(CheckpointError):
            self.store.prepare(request,wrong)

    def test_preparation_reply_is_durable_and_idempotent(self):
        request=self.request("prepare")
        result=self.store.prepare(request,self.store.head())
        self.store.close()
        self.store=CheckpointStore(self.path,"relay-authority","scope-1",b"k"*32)
        self.assertEqual(result,self.store.prepare(request,self.store.head()))

    def test_request_id_cannot_change_payload(self):
        request=self.request("prepare")
        self.store.prepare(request,self.store.head())
        with self.assertRaises(CheckpointError):
            self.store.prepare(dict(request,binding=dict(B,grant_token="grant-2")),self.store.head())

    def test_old_prepare_reply_rejected_after_new_binding(self):
        old=self.request("prepare")
        self.store.prepare(old,self.store.head())
        self.store.prepare(dict(old,id="prepare-2",binding=dict(B,grant_token="grant-2")),
                           self.store.head())
        with self.assertRaises(CheckpointError):
            self.store.prepare(old,self.store.head())

    def test_activation_requires_observed_fence_not_caller_boolean(self):
        proof=self.store.prepare(self.request("prepare"),self.store.head())
        with self.assertRaises(CheckpointError):
            self.store.activate(self.request("activate",checkpoint=proof["state"]["checkpoint"],
                                             fence_ack={"fenced":True}))

    def test_activation_lost_reply_returns_retained_receipt(self):
        proof=self.store.prepare(self.request("prepare"),self.store.head())
        checkpoint=proof["state"]["checkpoint"]
        fence=self.store.record_fence(B,checkpoint,{"fenced":True,"generation":185})
        request=self.request("activate",checkpoint=checkpoint,fence_ack=fence)
        result=self.store.activate(request)
        self.store.close()
        self.store=CheckpointStore(self.path,"relay-authority","scope-1",b"k"*32)
        self.assertEqual(result,self.store.activate(request))
        self.assertEqual(self.store.phase()["phase"],"active")

    def test_activation_rejects_changed_fence_binding(self):
        proof=self.store.prepare(self.request("prepare"),self.store.head())
        checkpoint=proof["state"]["checkpoint"]
        fence=self.store.record_fence(B,checkpoint,{"fenced":True,"generation":185})
        with self.assertRaises(CheckpointError):
            self.store.activate(self.request("activate",checkpoint=checkpoint,
                                             fence_ack=dict(fence,grant_token="old")))


    def test_unpublished_draft_does_not_destroy_witnessed_packet(self):
        original=self.store.head()
        draft=self.store.advance({"records":[["guards","relay",{"resource":"relay","generation":190}]]},B)
        self.assertEqual(self.store.get(original),self.genesis)
        self.store.select_witnessed(original)
        self.assertEqual(self.store.head(),original)
        next_packet=self.store.advance({"records":[]},dict(B,grant_token="grant-2"))
        self.assertGreater(next_packet["payload"]["sequence"],draft["payload"]["sequence"])
        self.assertEqual(self.store.generation_floors(["relay"])["relay"],190)

    def test_replica_retains_uncommitted_branch_without_blessing_it(self):
        primary=self.store.advance({"records":[["guards","relay",{"resource":"relay","generation":185}]]},B)
        branch=self.store._sign(dict(primary["payload"],binding=dict(B,grant_token="grant-2"),
                                    state={"records":[]}))
        self.store.retain(branch)
        self.assertEqual(self.store.head(),checkpoint_ref(primary))
        self.assertEqual(self.store.get(checkpoint_ref(branch)),branch)
        self.store.select_witnessed(checkpoint_ref(branch))
        self.assertEqual(self.store.head(),checkpoint_ref(branch))
        self.assertEqual(self.store.generation_floors(["relay"])["relay"],185)

    def test_published_pruning_keeps_floor_and_current_payloads(self):
        for i in range(7):
            packet=self.store.advance({"records":[["_fence_generations","relay",200-i]]},B)
            self.store.published(checkpoint_ref(packet))
        self.assertLessEqual(len(self.store.retained()),2)
        self.assertEqual(self.store.generation_floors(["relay"])["relay"],200)


class Freshness(unittest.TestCase):
    def setUp(self):
        self.head=dict(service_id="relay-authority",sequence=12,digest="a"*64)
        self.witness=dict(self.head,boot_epoch="epoch-1")

    def select(self,witness,heads,epoch="epoch-1"):
        return select_current("relay-authority",witness,heads,["pi","mac"],epoch)

    def test_witness_allows_current_survivor(self):
        result=self.select(self.witness,{"mac":self.head})
        self.assertEqual(result["source"],"mac")
        self.assertFalse(result["reestablish"])

    def test_witness_rejects_stale_survivor(self):
        with self.assertRaises(CheckpointError):
            self.select(self.witness,{"mac":dict(self.head,sequence=11)})

    def test_missing_witness_requires_both_replicas(self):
        with self.assertRaises(CheckpointError):
            self.select(None,{"mac":self.head})

    def test_restart_reconciliation_chooses_highest_retained_sequence(self):
        result=self.select(None,{"pi":dict(self.head,sequence=11,digest="b"*64),
                                 "mac":self.head})
        self.assertEqual(result["source"],"mac")
        self.assertTrue(result["reestablish"])

    def test_equal_sequence_conflict_never_selects(self):
        with self.assertRaises(CheckpointError):
            self.select(None,{"pi":dict(self.head,digest="b"*64),"mac":self.head})

    def test_old_epoch_witness_never_certifies_current(self):
        with self.assertRaises(CheckpointError):
            self.select(self.witness,{"mac":self.head},"epoch-2")


if __name__ == "__main__":
    unittest.main()
