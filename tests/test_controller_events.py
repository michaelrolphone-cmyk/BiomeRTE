"""Managed event registration, cursor continuity, replication and sender fencing."""
import base64
import copy
import io
import json
import os
import tempfile
import types
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from biomerte.app import Application, initialize
from biomerte.controller_authority import ControllerAuthority
from biomerte.controller_events import ManagedEvents, EVENT_KINDS
from biomerte.identity import generate, fingerprint, sign
from biomerte.mcp_events import EventStore, EVENT, CallbackError
from biomerte.grok_receivers import mutate
from biomerte.store import Fault, digest

PI, MAC = "a"*64, "b"*64
B = dict(service_id="site-controller",manifest_revision="revision",worker_id="mac",
         worker_incarnation="mac-1",boot_epoch="boot",grant_token="grant")


class ManagedEventTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys=tempfile.TemporaryDirectory()
        cls.key=os.path.join(cls.keys.name,"operator.key")
        cls.public=generate(cls.key)
        cls.actor=fingerprint(cls.public)

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        conf=initialize(self.tmp.name,"managed-events")
        self.config=dict(authority_kind="controller",service_id=B["service_id"],
            authority_id=conf["node"],site=conf["site"],controller_aliases=[PI,MAC],
            operator_actors=[self.actor],operator_actor_aliases={self.actor:"admin"},
            event_default_controller=PI)
        self.app=Application(self.tmp.name,controller_runtime=self.config)
        self.authority=ControllerAuthority(self.app,self.config,B)
        self.addCleanup(self.close)
        self.messages=[]
        self.blocked=False
        self.post_status=200
        self.barriers=[]
        def seed(tx):
            tx.put("peers",self.actor,dict(id=self.actor,key=self.public,enabled=True,scopes=[]))
            tx.put("_meta","events",100)
            tx.put("_meta","delegation_sequence",10)
            for source, high in ((PI,7),(MAC,3)):
                tx.put("_controller_source_feeds",source,dict(source_controller=source,
                    events={"high":20,"logical_high":100,"floor":0},
                    delegation_events={"high":high,"logical_high":10,"floor":0}))
            original=self.event(7,"original")
            tx.put("_controller_source_delegation_events",PI+":%020d"%7,original)
        self.app.stores["site"].execute(seed)
        self.daemon=types.SimpleNamespace(app=self.app,config=self.config,binding=B)
        self.events=self.open()
        self.authority.events=self.events
        self.secret="whsec_"+base64.b64encode(b"fixture-webhook-key-"*2).decode()

    def close(self):
        self.events.close()
        for store in self.app.stores.values():
            store.close()

    @staticmethod
    def event(sequence, key):
        return dict(seq=sequence,target="chatgpt",data=dict(delegation_id=key,
            run_id="run-"+key,workflow="test-workflow",activity="delegate",realm="site"))

    def barrier(self,publish):
        if self.blocked:
            raise Fault("Current owner grant is unavailable",503)
        self.barriers.append(publish)
        if publish:
            self.authority.snapshot()

    def post(self,url,headers,body):
        value=json.loads(body)
        self.messages.append((url,dict(headers),value))
        if value.get("type")=="verification":
            return 200,json.dumps({"challenge":value["challenge"]}).encode()
        return self.post_status,b""

    def open(self):
        return ManagedEvents(self.daemon,post=self.post,
                             authorize=lambda owner,grant:True,barrier=self.barrier)

    def envelope(self,operation,args,request):
        body=dict(site=self.config["site"],realm="site",operation=operation,
                  arguments=args,id=request,controller=PI)
        return dict(body,auth={"actor":self.actor,"signature":sign(self.key,body)})

    def params(self,controller=PI,realm="site",cursor="6"):
        return dict(name=EVENT,arguments={"realm":realm},controller=controller,
                    delivery={"mode":"webhook","url":"https://receiver.example/callback","secret":self.secret},
                    cursor=cursor)

    def subscribe(self,request="subscribe",controller=PI,cursor="6"):
        args=dict(controller=controller,method="events/subscribe",
                  params=self.params(controller,cursor=cursor),owner="owner",grant="oauth-grant")
        return self.authority.call(self.envelope("gateway.events.change",args,request),False)

    def saved(self,controller,sub):
        return self.app.stores["site"].execute(lambda tx:
            tx.need("_gateway_event_subscriptions",digest([controller,sub])),read_only=True)

    def test_original_subscription_id_secret_and_cursor_survive_projection_restart(self):
        result=self.subscribe()
        saved=self.saved(PI,result["id"])
        self.assertEqual(saved["record"]["key"],self.secret)
        self.assertEqual(saved["record"]["cursor"],6)
        self.assertEqual(json.loads(saved["record"]["args"])["controller"],PI)
        self.events.close()
        self.events=self.open()
        self.authority.events=self.events
        row=self.events.stores[PI].db.execute("SELECT * FROM subscriptions WHERE id=?",(result["id"],)).fetchone()
        self.assertEqual(dict(row),saved["record"])
        self.assertEqual(self.subscribe(),result)

    def test_old_cursor_continues_into_new_logical_events_without_rewriting_ids(self):
        result=self.subscribe()
        def new_event(tx):
            tx.put("delegation_events","%020d"%11,self.event(11,"new-logical"))
            tx.put("_meta","delegation_sequence",11)
        self.app.stores["site"].execute(new_event)
        self.messages=[]
        self.assertEqual(self.events.feed(PI,"site",None,6,16)["high"],8)
        self.events.deliver()
        self.assertEqual(self.messages[0][2]["eventId"],"delegate_original")
        self.assertEqual(self.messages[0][2]["cursor"],"7")
        self.assertEqual(self.saved(PI,result["id"])["record"]["cursor"],7)
        self.events.deliver()
        self.assertEqual(self.messages[1][2]["eventId"],"delegate_new-logical")
        self.assertEqual(self.messages[1][2]["cursor"],"8")
        self.assertEqual(self.messages[1][2]["controller"],PI)
        self.assertEqual(self.saved(PI,result["id"])["record"]["cursor"],8)

    def test_same_subscription_identity_on_two_sources_has_separate_canonical_ownership(self):
        first=self.subscribe("pi",PI,"6")
        second=self.subscribe("mac",MAC,"3")
        self.assertNotEqual(first["id"],second["id"])
        self.assertEqual(self.saved(PI,first["id"])["source_controller"],PI)
        self.assertEqual(self.saved(MAC,second["id"])["source_controller"],MAC)
        self.assertEqual(self.saved(MAC,second["id"])["record"]["cursor"],3)

    def test_callback_requires_current_grant_and_checkpoint_barrier(self):
        result=self.subscribe()
        self.messages=[]
        self.blocked=True
        with self.assertRaises(Fault):
            self.events.deliver()
        self.assertEqual(self.messages,[])
        self.assertEqual(self.saved(PI,result["id"])["record"]["cursor"],6)
        self.blocked=False
        self.barriers=[]
        self.events.deliver()
        self.assertIn(True,self.barriers)
        self.assertEqual(self.saved(PI,result["id"])["record"]["cursor"],7)

    def test_authorization_transport_failure_preserves_the_replicated_cursor(self):
        sub = self.subscribe()['id']
        self.messages.clear()

        def unavailable(owner, grant):
            raise HTTPError('https://controller.example/authorize', 403, 'Forbidden', {}, None)

        self.events._authorize_override = unavailable
        self.assertEqual(self.events.deliver(), 1)
        self.assertEqual(self.events.deliver(), 0)
        self.assertEqual(self.messages, [])
        pending = self.saved(PI, sub)['record']
        self.assertEqual(pending['cursor'], 6)
        self.assertEqual(pending['last_error'], 'authorization HTTP 403')

        self.events.close()
        self.events = self.open()
        self.authority.events = self.events
        store = self.events.stores[PI]
        with store.db:
            store.db.execute('UPDATE subscriptions SET next_try=0 WHERE id=?', (sub,))
        self.assertEqual(self.events.deliver(), 1)
        self.assertEqual(self.messages[0][2]['eventId'], 'delegate_original')
        self.assertEqual(self.saved(PI, sub)['record']['cursor'], 7)

    def test_authorization_request_identifies_the_event_client(self):
        with tempfile.NamedTemporaryFile() as key:
            key.write(b'qualification-key-' * 3)
            key.flush()
            self.events._authorize_override = None
            self.config.update(event_authorize_url='https://controller.example/authorize',
                               event_authorize_key_file=key.name)
            with patch('biomerte.controller_events.build_opener') as opener:
                opener.return_value.open.return_value = io.BytesIO(b'{"allowed": false}')
                self.assertFalse(self.events.authorize('non-owner', None))

        request = opener.return_value.open.call_args.args[0]
        self.assertEqual(request.get_header('User-agent'), 'BiomeRTE-Events/1.0')
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + 'qualification-key-' * 3)
        self.assertEqual(json.loads(request.data), {'owner': 'non-owner', 'grant': None})
        self.assertEqual(self.barriers, [False])

    def test_retry_after_uncertain_callback_keeps_exact_event_identity(self):
        result=self.subscribe()
        self.messages=[]
        self.post_status=503
        self.events.deliver()
        first=self.messages[-1][2]["eventId"]
        self.assertEqual(self.saved(PI,result["id"])["record"]["cursor"],6)
        self.events.close()
        self.events=self.open()
        self.authority.events=self.events
        store=self.events.stores[PI]
        with store.db:
            store.db.execute("UPDATE subscriptions SET next_try=0 WHERE id=?",(result["id"],))
        self.post_status=200
        self.events.deliver()
        self.assertEqual(self.messages[-1][2]["eventId"],first)
        self.assertEqual(self.saved(PI,result["id"])["record"]["cursor"],7)

    def test_owner_or_payload_tampering_cannot_change_subscription(self):
        args=dict(controller=PI,method="events/subscribe",params=self.params(),owner="owner",grant=None)
        envelope=self.envelope("gateway.events.change",args,"tamper")
        envelope["arguments"]["owner"]="another-owner"
        with self.assertRaises(Fault):
            self.authority.call(envelope,False)
        count=self.app.stores["site"].execute(lambda tx:len(tx.rows("_gateway_event_subscriptions")),read_only=True)
        self.assertEqual(count,0)
        self.config["operator_actors"]=[]
        # The runtime's explicit immutable operator set remains the source of truth.
        self.app.controller_operator_actors=frozenset()
        with self.assertRaises(Fault):
            self.authority.call(self.envelope("gateway.events.change",args,"not-operator"),False)

    def test_grok_registration_receipt_is_canonical_and_replays_after_restart(self):
        args=dict(controller=MAC,action="register",owner="owner",grant="grant",
                  args={"receiver":"robot","request_id":"receiver-1","realm":"site",
                        "url":"https://receiver.example/grok","key":"fixture-bearer"})
        envelope=self.envelope("gateway.events.grok",args,"grok-native-request")
        result=self.authority.call(envelope,False)
        kinds=self.app.stores["site"].execute(lambda tx:[len(tx.rows(kind)) for kind in
            ("_gateway_event_subscriptions","_gateway_event_receipts")],read_only=True)
        self.assertEqual(kinds,[1,1])
        self.events.close();self.events=self.open();self.authority.events=self.events
        self.assertEqual(self.authority.call(envelope,False),result)

    def test_local_subscriptions_are_rejected_by_managed_runtime(self):
        args=dict(controller=PI,method="events/subscribe",params=self.params(realm="local"),
                  owner="owner",grant=None)
        with self.assertRaises(Fault):
            self.authority.call(self.envelope("gateway.events.change",args,"local"),False)

    def test_all_registration_and_delivery_rows_enter_incremental_checkpoint(self):
        result=self.subscribe()
        self.events.deliver()
        root=self.authority.snapshot()
        kinds={row["kind"] for row in root["shards"]}
        self.assertIn("_gateway_event_subscriptions",kinds)
        self.assertIn("_receipt",kinds)
        retained=[]
        for shard in root["shards"]:
            if shard["kind"] in EVENT_KINDS.values():
                retained.extend(json.loads(self.authority.snapshot_chunk(shard["digest"])))
        self.assertEqual(retained[0][2]["record"]["cursor"],7)

    def test_frozen_native_event_store_keeps_local_realm_available(self):
        enabled={"site":True,"local":True}
        path=os.path.join(self.tmp.name,"legacy-events.sqlite")
        native=EventStore(path,lambda realm,after,limit:{"events":[],"high":7},
                          lambda owner,grant:True,self.post,controller=PI,
                          realm_allowed=lambda realm:enabled[realm])
        self.addCleanup(native.close)
        native.change("events/subscribe",self.params(),"owner",None)
        enabled["site"]=False
        with self.assertRaises(ValueError):
            native.change("events/subscribe",self.params(),"owner",None)
        native.change("events/subscribe",self.params(realm="local"),"owner",None)
        with self.assertRaises(ValueError):
            mutate(native,"register",{"receiver":"r","request_id":"r1","realm":"site",
                   "url":"https://receiver.example/grok","key":"fixture"},"owner",None)
        self.messages=[]
        native.deliver()
        self.assertEqual(self.messages,[])


if __name__=="__main__":
    unittest.main()
