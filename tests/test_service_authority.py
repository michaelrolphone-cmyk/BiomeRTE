import json
import os
import tempfile
import time
import unittest
from biomerte.app import Application, initialize
from biomerte.api import Context
from biomerte import providers
from biomerte.identity import generate, fingerprint, sign
from biomerte.store import Fault
from biomerte.service_authority import ScopedAuthority

B=dict(service_id="relay-authority",manifest_revision="r1",worker_id="pi",
       worker_incarnation="pi-1",boot_epoch="epoch-1",grant_token="grant-1")


class AuthorityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys=tempfile.TemporaryDirectory()
        cls.provider_key=os.path.join(cls.keys.name,"provider.key")
        cls.pub=generate(cls.provider_key)
        cls.actor=fingerprint(cls.pub)

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        conf=initialize(self.tmp.name,"site-one")
        self.app=Application(self.tmp.name)
        self.resource="relay.test"
        self.config=dict(service_id="relay-authority",authority_id=conf["node"],
                         site="site-one",resources=[self.resource],
                         providers=[self.actor],contracts=["relay.test:1"],
                         fence_contracts={self.resource:"relay.test:1"})
        def seed(tx):
            ctx=Context(self.app,scope="site")
            providers.define(tx,ctx,"relay.test",{}, {},version=1)
            offer=providers.offer(tx,ctx,self.actor,"relay.test:1",self.resource,
                                 configuration={},capacity=1,lease_class="persistent")
            tx.put("peers",self.actor,dict(id=self.actor,key=self.pub,enabled=True,
                   scopes=["cap.offer","provider.poll","cap.fenced","provider.result",
                           "cap.status","offer:relay.test:1","resource:"+self.resource]))
            tx.put("_fence_generations",self.resource,183)
            tx.put("guards",self.resource,dict(holder="",generation=184,resource=self.resource))
            return offer
        self.offer=self.app.stores["site"].execute(seed)
        self.authority=ScopedAuthority(self.app,self.config,B)

    def tearDown(self):
        for store in self.app.stores.values():
            store.close()
        self.tmp.cleanup()

    def envelope(self,operation,args,request="request-1",controller=None):
        body=dict(site="site-one",realm="site",operation=operation,arguments=args,id=request)
        if controller is not None:
            body["controller"]=controller
        return dict(body,auth=dict(actor=self.actor,signature=sign(self.provider_key,body)))

    def acknowledge(self,marker):
        for lease in marker["leases"].values():
            row=self.app.stores["site"].execute(lambda tx:tx.need("leases",lease),read_only=True)
            env=self.envelope("cap.fenced",dict(provider=self.actor,lease=lease,
                              generation=row["generation"]),"ack-"+lease)
            self.authority.call(env,preparing=True)

    def test_signed_conditional_withdrawal_obeys_exact_logical_contract_scope(self):
        args=dict(provider=self.actor,resource=self.resource,service=self.offer['service'],
                  registrations={'relay.test:1':self.offer['registration']})
        bad=dict(args,registrations={**args['registrations'],'foreign:1':None})
        with self.assertRaisesRegex(Fault,'logical contract scope'):
            self.authority.call(self.envelope('cap.withdraw',bad,'foreign'),preparing=False)
        result=self.authority.call(self.envelope('cap.withdraw',args,'shutdown'),preparing=False)
        self.assertEqual(result['withdrawn'],['relay.test:1'])

    def test_former_native_operation_cannot_be_recreated(self):
        def history(tx):
            tx.put("_service_source","initial",{
                "source_controller":"original-pi",
                "former_native_records":[["_submissions","old-led-operation",
                                            {"task":"old-label","inputs":{}}]],
                "replay_allowed":False})
            peer=tx.need("peers",self.actor)
            peer["scopes"]+=["cap.submit","cap.invoke"]
            tx.put("peers",self.actor,peer)
        self.app.stores["site"].execute(history)
        for name in ("cap.submit","cap.invoke"):
            with self.subTest(operation=name):
                args=dict(operation="old-led-operation",resource=self.resource,
                          contract="relay.test:1",inputs={})
                with self.assertRaises(Fault) as raised:
                    self.authority.call(self.envelope(name,args,"retry-"+name),False)
                self.assertEqual(raised.exception.status,409)
                self.assertIn("original-pi",str(raised.exception))
                self.assertIn("replay_allowed=false",str(raised.exception))
        counts=self.app.stores["site"].execute(lambda tx:[
            tx.db.execute("SELECT count(*) FROM records WHERE kind=?",(kind,)).fetchone()[0]
            for kind in ("leases","actions","_submission_active","_submission_leases")],
            read_only=True)
        self.assertEqual(counts,[0,0,0,0])

    def test_new_reservation_can_reuse_a_former_task_label(self):
        def history(tx):
            tx.put("_service_source","initial",{
                "source_controller":"original-pi",
                "former_native_records":[["_submissions","old-task-label",{}],
                                          ["leases","old-lease",{"task":"old-task-label"}]],
                "replay_allowed":False})
            peer=tx.need("peers",self.actor)
            peer["scopes"]+=["cap.acquire"]
            tx.put("peers",self.actor,peer)
        self.app.stores["site"].execute(history)
        result=self.authority.call(self.envelope("cap.acquire",{
            "contract":"relay.test:1","resource":self.resource,"task":"old-task-label"},
            "fresh-reservation"),False)
        self.assertEqual(result["resource"],self.resource)
        self.assertNotEqual(result["id"],"old-lease")

    def test_generation_uses_maximum_retained_state(self):
        marker=self.authority.prepare_fences("a"*64)
        lease=self.app.stores["site"].execute(
            lambda tx:tx.need("leases",marker["leases"][self.resource]),read_only=True)
        self.assertEqual(lease["generation"],185)

    def test_multiple_capacity_resource_is_rejected(self):
        self.app.stores["site"].execute(lambda tx:tx.put("_capacity",self.resource,2))
        with self.assertRaises(Fault):
            self.authority.prepare_fences("a"*64)

    def test_retained_draft_floor_is_preserved(self):
        marker=self.authority.prepare_fences("a"*64,{self.resource:190})
        lease=self.app.stores["site"].execute(lambda tx:tx.need("leases",marker["leases"][self.resource]),read_only=True)
        self.assertEqual(lease["generation"],191)

    def test_preparation_retry_does_not_increment_fence(self):
        self.assertEqual(self.authority.prepare_fences("a"*64),
                         self.authority.prepare_fences("a"*64))

    def test_preparation_poll_contains_only_exact_service_fence(self):
        marker=self.authority.prepare_fences("a"*64)
        result=self.authority.call(self.envelope("provider.poll",{"provider":self.actor}),True)
        self.assertEqual([x["id"] for x in result["fences"]],list(marker["leases"].values()))
        self.assertEqual(result["actions"],[])
        self.assertEqual(result["streams"],[])
        self.assertEqual(result["activations"],[])

    def test_claim_and_unsigned_calls_are_rejected_during_preparation(self):
        with self.assertRaises(Fault):
            self.authority.call(self.envelope("provider.claim",
                dict(provider=self.actor,operation="action",generation=185)),True)
        env=self.envelope("provider.poll",dict(provider=self.actor))
        env.pop("auth")
        with self.assertRaises(Fault):
            self.authority.call(env,True)

    def test_signature_is_verified_by_native_application(self):
        self.authority.prepare_fences("a"*64)
        env=self.envelope("provider.poll",dict(provider=self.actor))
        env["arguments"]["limit"]=1
        with self.assertRaises(Fault):
            self.authority.call(env,True)

    def test_explicit_other_controller_is_rejected(self):
        env=self.envelope("provider.poll",dict(provider=self.actor),controller="other")
        with self.assertRaises(Fault):
            self.authority.call(env,False)

    def test_out_of_scope_resource_rejected(self):
        env=self.envelope("cap.acquire",dict(contract="relay.test:1",
                          resource="other-device",task="test"))
        with self.assertRaises(Fault):
            self.authority.call(env,False)

    def test_fence_is_not_ready_until_signed_board_ack(self):
        self.authority.prepare_fences("a"*64)
        with self.assertRaises(Fault):
            self.authority.finish_fences()

    def test_fence_ack_finishes_and_preserves_floor(self):
        marker=self.authority.prepare_fences("a"*64)
        self.acknowledge(marker)
        ack=self.authority.finish_fences()
        self.assertTrue(ack["fenced"])
        self.assertEqual(ack["resources"][self.resource]["generation"],185)
        self.assertEqual(ack,self.authority.finish_fences())
        state=self.app.stores["site"].execute(lambda tx:(
            tx.need("_fence_generations",self.resource),tx.need("guards",self.resource)),read_only=True)
        self.assertEqual(state[0],185)
        self.assertEqual(state[1]["holder"],"")

    def test_unknown_action_is_retained_without_replay(self):
        def old(tx):
            tx.put("leases","old",dict(id="old",resource=self.resource,provider=self.actor,
                   offer=self.offer["id"],contract="relay.test:1",state="bound",
                   generation=184,guard=self.resource,owner="operator"))
            tx.put("actions","uncertain-action",dict(id="uncertain-action",
                   resource=self.resource,provider=self.actor,lease="old",
                   contract="relay.test:1",generation=184,state="claimed",inputs={}))
            tx.put("_stream_grants","old-stream",dict(id="old-stream",
                   resource=self.resource,state="open",expires_at=time.time()+3600))
        self.app.stores["site"].execute(old)
        marker=self.authority.prepare_fences("a"*64)
        self.acknowledge(marker)
        self.authority.finish_fences()
        action,lease,stream=self.app.stores["site"].execute(lambda tx:(
            tx.need("actions","uncertain-action"),tx.need("leases","old"),
            tx.need("_stream_grants","old-stream")),read_only=True)
        self.assertEqual(action["state"],"uncertain")
        self.assertFalse(action["recovery"]["replay_allowed"])
        self.assertEqual(action["generation"],184)
        self.assertEqual(lease["state"],"released")
        self.assertEqual(stream["state"],"revoked")


if __name__=="__main__":
    unittest.main()
