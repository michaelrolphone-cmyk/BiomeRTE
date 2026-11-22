"""Publication failure must not expose commands, results or pairing state."""
import asyncio
import json
import os
import tempfile
import types
import unittest

try:
    from biomerte.service_daemon import AuthorityDaemon, scope_digest
except ModuleNotFoundError as error:
    raise unittest.SkipTest("Controller daemon boundary tests require aiohttp") from error

from aiohttp import WSMsgType

from biomerte.app import Application, initialize
from biomerte.controller_authority import ControllerAuthority
from biomerte.identity import generate, fingerprint, sign
from biomerte.service_checkpoint import digest as checkpoint_digest
from biomerte.store import Fault

B = dict(service_id="site-controller",manifest_revision="revision",worker_id="mac",
         worker_incarnation="mac-1",boot_epoch="boot",grant_token="grant")


class ControllerDaemonTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys=tempfile.TemporaryDirectory()
        cls.key=os.path.join(cls.keys.name,"proxy.key")
        cls.public=generate(cls.key)
        cls.actor=fingerprint(cls.public)

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        conf=initialize(self.tmp.name,"controller-boundary")
        self.config=dict(authority_kind="controller",service_id=B["service_id"],
            authority_id=conf["node"],site=conf["site"],
            controller_aliases=["physical-pi","physical-mac"],
            operator_actors=[self.actor],operator_actor_aliases={self.actor:"admin"})
        self.app=Application(self.tmp.name,controller_runtime=self.config)
        self.addCleanup(self.close)
        self.authority=ControllerAuthority(self.app,self.config,B)
        self.app.stores["site"].execute(lambda tx: tx.put("peers",self.actor,
            dict(id=self.actor,key=self.public,enabled=True,scopes=[])))
        self.daemon=AuthorityDaemon.__new__(AuthorityDaemon)
        self.daemon.config=self.config
        self.daemon.binding=B
        self.daemon.app=self.app
        self.daemon.authority=self.authority
        self.daemon.witnessed_vision_inspections={}
        self.daemon.lock=asyncio.Lock()
        self.published=0
        self.validated=0
        self.fail_publish=0
        self.fail_validate=0
        self.current_phase="active"
        async def validate(binding,preparing=True):
            self.validated+=1
            if self.validated==self.fail_validate:
                raise Fault("Grant revoked",503)
            return {"valid":True}
        async def publish():
            self.published+=1
            if self.published==self.fail_publish:
                raise Fault("Missing replica acknowledgement",503)
            return {"checkpoint":"fixture"}
        async def phase():
            # The real private adapter validates the binding before returning
            # its phase. Revocation on the final fence must still hide a result.
            await validate(B)
            return {"phase":self.current_phase}
        self.daemon.transport=types.SimpleNamespace(validate=validate)
        self.daemon.publish=publish
        self.daemon.phase=phase

    def close(self):
        for store in self.app.stores.values():
            store.close()

    def message(self,key="boundary",request="request-1"):
        body=dict(site=self.config["site"],realm="site",controller="physical-pi",
            operation="knowledge.put",arguments={"key":key,"body":{"value":1}},id=request)
        envelope=dict(body,auth=dict(actor=self.actor,signature=sign(self.key,body)))
        return dict(type="call",id="rpc-1",binding=B,envelope=envelope)

    def document(self,key="boundary"):
        return self.app.stores["site"].execute(lambda tx: tx.get("documents",key),read_only=True)

    def test_managed_store_can_use_unmapped_full_durability_rollback_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = initialize(directory, "controller-rollback-journal")
            config = dict(self.config, authority_id=identity["node"],
                          site=identity["site"], site_rollback_journal=True)
            app = Application(directory, controller_runtime=config)
            try:
                site = app.stores["site"]
                local = app.stores["local"]
                with site.connection() as db:
                    self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "delete")
                    self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], 2)
                    self.assertEqual(db.execute("PRAGMA mmap_size").fetchone()[0], 0)
                with local.connection() as db:
                    self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                authority = ControllerAuthority(app,config,B)
                site.execute(lambda tx:tx.put("documents","persistent",{"id":"persistent"}))
                snapshot = authority.snapshot()
                for shard in snapshot["shards"]:
                    self.assertIsInstance(authority.snapshot_chunk(shard["digest"]), bytes)
                self.assertFalse(os.path.exists(site.path+"-shm"))
            finally:
                for store in app.stores.values():
                    store.close()

    async def test_managed_health_requires_exact_active_phase_and_registered_link(self):
        self.daemon.connected = True
        self.current_phase = "prepared"
        self.assertEqual((await self.daemon.health(None)).status, 503)
        self.current_phase = "active"
        before = self.validated
        self.assertEqual((await self.daemon.health(None)).status, 200)
        self.assertEqual(self.validated-before, 1)
        self.fail_validate = self.validated+1
        self.assertEqual((await self.daemon.health(None)).status, 503)
        self.daemon.connected = False
        self.assertEqual((await self.daemon.health(None)).status, 503)

    async def test_read_batch_verifies_each_actor_and_shares_publication(self):
        import copy
        signed = self.message()["envelope"]
        signed["operation"] = "cap.discover"
        signed["arguments"] = {}
        body = {key:value for key,value in signed.items() if key != "auth"}
        signed["auth"]["signature"] = sign(self.key, body)
        denied = copy.deepcopy(signed)
        denied["auth"]["signature"] = self.message("different")["envelope"]["auth"]["signature"]
        messages = [dict(self.message(), envelope=value, id=str(i))
                    for i,value in enumerate((signed, denied))]
        replies = await self.daemon.dispatch_managed_reads(messages)
        self.assertEqual([row["status"] for row in replies], [200,400])
        self.assertEqual(self.published, 2)
        self.assertNotIn("result", replies[1])
        self.fail_validate = self.validated+2
        replies = await self.daemon.dispatch_managed_reads(messages)
        self.assertEqual([row["status"] for row in replies], [503,503])
        self.assertTrue(all("result" not in row for row in replies))

    async def test_registry_batch_does_not_expose_result_before_acknowledgement(self):
        messages = []
        for i in range(2):
            message = self.message(request="definition-" + str(i))
            message["id"] = "rpc-" + str(i)
            envelope = message["envelope"]
            envelope["operation"] = "cap.define"
            envelope["arguments"] = dict(name="diagnostic_fixture_" + str(i),
                version=1, inputs={}, outputs={})
            envelope["auth"]["signature"] = sign(self.key, {
                key:value for key,value in envelope.items() if key != "auth"})
            messages.append(message)
        self.fail_publish = 2
        replies = await self.daemon.dispatch_managed_reads(messages)
        self.assertEqual([row["status"] for row in replies], [503,503])
        self.assertTrue(all("result" not in row for row in replies))
        replies = await self.daemon.dispatch_managed_reads(messages)
        self.assertEqual([row["status"] for row in replies], [200,200])

    async def test_only_current_unchanged_witnessed_vision_inspection_uses_fast_path(self):
        authority = self.config["authority_id"]
        grant = dict(id="vision", authority=authority, contract="robot.vision.stream:1",
                     state="open", generation=2, consumers=["viewer"])
        message = dict(type="inspect", id="rpc", authority=authority, session=grant["id"], binding=B)
        self.authority.inspect = lambda session, issuer: dict(grant)
        self.assertIsNone(await self.daemon.recheck_witnessed_vision_inspection(message))
        self.daemon.remember_vision_inspection(message, dict(status=200, grant=dict(grant)))
        self.assertEqual((await self.daemon.recheck_witnessed_vision_inspection(message))["grant"], grant)
        self.assertEqual(self.published, 0)
        self.assertIsNone(await self.daemon.recheck_witnessed_vision_inspection(
            dict(message, preparation=True)))
        self.current_phase = "prepared"
        self.assertIsNone(await self.daemon.recheck_witnessed_vision_inspection(message))
        self.current_phase = "active"
        grant["consumers"] = ["replacement"]
        self.assertIsNone(await self.daemon.recheck_witnessed_vision_inspection(message))
        grant["consumers"] = ["viewer"]
        self.fail_validate = self.validated+2
        self.assertIsNone(await self.daemon.recheck_witnessed_vision_inspection(message))

    async def test_witnessed_vision_attach_checks_signed_call_without_caching_credential(self):
        authority = self.config["authority_id"]
        grant = dict(id="vision", authority=authority, contract="robot.vision.stream:1",
                     state="open", generation=2, consumers=["viewer"])
        descriptor = dict(grant, role="provider", url="wss://example.test/vision",
                          attachment_credential="fresh-credential")
        message = self.message()
        envelope = message["envelope"]
        envelope["operation"] = "stream.attach"
        envelope["arguments"] = {"session": "vision", "role": "provider"}
        self.daemon.remember_vision_inspection(
            dict(message, envelope=dict(envelope, operation="stream.open")),
            dict(status=200, result=descriptor))
        self.assertEqual(self.daemon.witnessed_vision_inspections[(authority, "vision")], grant)
        def attach(received, preparing):
            self.assertIs(received, envelope)
            self.assertFalse(preparing)
            return descriptor
        self.authority.call = attach
        reply = await self.daemon.recheck_witnessed_vision_inspection(message)
        self.assertEqual(reply["result"], descriptor)
        self.assertEqual(self.published, 0)
        descriptor["generation"] += 1
        self.assertIsNone(await self.daemon.recheck_witnessed_vision_inspection(message))
        def reject(received, preparing):
            raise Fault("Revoked credential",403)
        self.authority.call = reject
        self.assertIsNone(await self.daemon.recheck_witnessed_vision_inspection(message))

    async def test_socket_rejects_excess_work_and_drops_waiter_on_disconnect(self):
        started, release, rejected = asyncio.Event(), asyncio.Event(), asyncio.Event()
        processed, replies = [], []

        async def dispatch(message):
            processed.append(message["id"])
            started.set()
            await release.wait()
            return {"id": message["id"], "type": "call_result", "status": 200}

        class Socket:
            closed = False

            async def __aiter__(self):
                for message in ({"type": "registered"}, {"type": "call", "id": "first"}):
                    yield types.SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps(message))
                await started.wait()
                for name in ("waiting", "excess"):
                    yield types.SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps(
                        {"type": "pair", "id": name}))
                await rejected.wait()
                self.closed = True

            async def send_json(self, response):
                replies.append(response)
                rejected.set()

        self.daemon.dispatch = dispatch
        socket = Socket()
        connection = asyncio.create_task(self.daemon.serve_controller_socket(socket))
        try:
            await asyncio.wait_for(rejected.wait(), 1)
            self.assertFalse(connection.done())
            self.assertEqual(replies, [{"id": "excess", "type": "pair_result",
                "binding": B, "status": 429, "error": "Managed controller is busy"}])
        finally:
            release.set()
            await connection
        self.assertEqual(processed, ["first"])

    async def test_lifecycle_request_is_admitted_during_registration_burst(self):
        started, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        processed = []

        async def dispatch(message):
            processed.append(message["id"])
            started.set()
            if message["id"] == "running":
                await release.wait()
            return {"id": message["id"], "type": "call_result", "status": 200}

        class Socket:
            closed = False

            async def __aiter__(self):
                def call(name, operation):
                    return types.SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps(
                        {"type": "call", "id": name, "envelope": {"operation": operation}}))

                yield call("running", "cap.offer")
                await started.wait()
                yield call("registration", "cap.define")
                yield call("diagnostic", "system.info")
                yield call("lifecycle", "provider.poll")
                yield call("lease-poll", "cap.lease")
                yield call("attachment", "stream.open")
                yield call("inspection", "stream.inspect")
                yield call("resume", "stream.resume")
                yield call("acquire-after-status", "cap.acquire")
                yield call("confirm-installed-fence", "cap.fenced")
                release.set()
                await completed.wait()
                self.closed = True

            async def send_json(self, response):
                if response["id"] == "confirm-installed-fence":
                    completed.set()

        self.daemon.dispatch = dispatch
        await asyncio.wait_for(self.daemon.serve_controller_socket(Socket()), 1)
        self.assertEqual(processed, ["running", "diagnostic", "attachment", "lifecycle", "inspection",
                                     "lease-poll", "resume", "registration",
                                     "acquire-after-status", "confirm-installed-fence"])

    async def test_discovery_and_grok_are_admitted_with_a_full_general_queue(self):
        started, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        replies = {}

        async def dispatch(message):
            if message["id"] == "running":
                started.set()
                await release.wait()
            return self.daemon.authority_response(message, B)

        class Socket:
            closed = False

            async def __aiter__(self):
                def call(request_id, operation):
                    return types.SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps({
                        "type": "call", "id": request_id,
                        "envelope": {"operation": operation},
                    }))

                yield call("running", "cap.invoke")
                await started.wait()
                yield call("general", "cap.invoke")
                yield call("excess", "cap.invoke")
                yield call("poll", "provider.poll")
                yield call("catalog", "system.discovery")
                yield call("grok-list", "gateway.events.grok")
                release.set()
                await completed.wait()
                self.closed = True

            async def send_json(self, response):
                replies[response["id"]] = response["status"]
                if len(replies) == 6:
                    completed.set()

        self.daemon.dispatch = dispatch
        await asyncio.wait_for(self.daemon.serve_controller_socket(Socket()), 1)
        self.assertEqual(replies, {"running": 200, "general": 200, "excess": 429,
                                   "poll": 200, "catalog": 200, "grok-list": 200})

    async def test_provider_can_advance_a_lease_with_the_general_queue_full(self):
        started, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        replies = {}
        batches = []
        calls = {
            "waiting": "cap.invoke",
            "excess": "cap.invoke",
            "submit": "cap.submit",
            "claim": "provider.claim",
            "heartbeat": "cap.heartbeat",
            "result": "provider.result",
            "reconcile": "provider.reconcile",
        }

        async def dispatch(message):
            if message["id"] == "running":
                started.set()
                await release.wait()
            return self.daemon.authority_response(message, B)

        async def dispatch_batch(messages):
            batches.append([message["id"] for message in messages])
            return [await dispatch(message) for message in messages]

        class Socket:
            closed = False

            async def __aiter__(self):
                def call(request_id, operation):
                    return types.SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps({
                        "type": "call", "id": request_id,
                        "envelope": {"operation": operation},
                    }))

                yield call("running", "cap.invoke")
                await started.wait()
                for request_id, operation in calls.items():
                    yield call(request_id, operation)
                release.set()
                await completed.wait()
                self.closed = True

            async def send_json(self, response):
                replies[response["id"]] = response["status"]
                if len(replies) == len(calls) + 1:
                    completed.set()

        self.daemon.dispatch = dispatch
        self.daemon.dispatch_managed_reads = dispatch_batch
        await asyncio.wait_for(self.daemon.serve_controller_socket(Socket()), 1)
        self.assertEqual(replies, {"running": 200, "waiting": 200, "excess": 429,
                                   "submit": 200, "claim": 200, "heartbeat": 200,
                                   "result": 200, "reconcile": 200})
        self.assertEqual(batches, [["submit", "claim", "heartbeat", "result", "reconcile"]])

    async def test_batched_submission_waits_for_publication_and_keeps_actor_checks(self):
        submission = self.message(request="submit-request")
        envelope = submission["envelope"]
        envelope.update(operation="cap.submit", arguments={"contract": "core.echo:1",
            "operation": "batch-echo", "inputs": {"value": "verified"}})
        envelope["auth"]["signature"] = sign(self.key, {
            key: value for key, value in envelope.items() if key != "auth"})
        denied = self.message(request="progress-request")
        denied["envelope"].update(operation="provider.reconcile", arguments={})

        self.fail_publish = 2
        replies = await self.daemon.dispatch_managed_reads([submission, denied])
        self.assertEqual([row["status"] for row in replies], [503, 503])
        self.assertTrue(all("result" not in row for row in replies))

        replies = await self.daemon.dispatch_managed_reads([submission, denied])
        self.assertEqual([row["status"] for row in replies], [200, 400])
        self.assertEqual(self.published, 4)
        action = self.app.stores["site"].execute(
            lambda tx: tx.get("actions", "batch-echo"), read_only=True)
        self.assertEqual(action["result"], {"value": "verified"})

    async def test_no_new_transaction_before_previous_state_is_published(self):
        self.fail_publish=1
        result=await self.daemon.dispatch(self.message())
        self.assertEqual(result["status"],503)
        self.assertNotIn("result",result)
        self.assertIsNone(self.document())

    async def test_lost_post_commit_ack_hides_result_and_same_id_replays_once(self):
        self.fail_publish=2
        result=await self.daemon.dispatch(self.message())
        self.assertEqual(result["status"],503)
        self.assertNotIn("result",result)
        committed=self.document()
        self.assertIsNotNone(committed)
        result=await self.daemon.dispatch(self.message())
        self.assertEqual(result["status"],200)
        self.assertEqual(self.document(),committed)
        self.assertEqual(len(self.document()["heads"]),1)

    async def test_revoked_grant_cannot_expose_committed_response(self):
        self.fail_validate=2
        result=await self.daemon.dispatch(self.message())
        self.assertEqual(result["status"],503)
        self.assertNotIn("result",result)
        self.assertIsNotNone(self.document())
        self.assertEqual(self.published,2)

    async def test_preparation_never_admits_site_commands(self):
        self.current_phase="prepared"
        result=await self.daemon.dispatch(self.message())
        self.assertEqual(result["status"],503)
        self.assertIsNone(self.document())

    async def test_pairing_is_public_pending_only_and_published_before_reply(self):
        message=dict(type="pair",id="pair-1",binding=B,operation="begin",
                     payload={"public_key":self.public,"label":"pending device"})
        response=await self.daemon.dispatch(message)
        self.assertEqual(response["type"],"pair_result")
        self.assertEqual(response["status"],200)
        row=self.app.stores["site"].execute(
            lambda tx: tx.need("candidates",self.actor),read_only=True)
        self.assertEqual(row["state"],"pending")
        rejected=await self.daemon.dispatch(dict(message,operation="approve"))
        self.assertEqual(rejected["status"],403)
        self.fail_publish=self.published+2
        failed=await self.daemon.dispatch(message)
        self.assertEqual(failed["status"],503)
        self.assertNotIn("result",failed)

    async def test_maintenance_does_not_tick_after_failed_publication(self):
        self.fail_publish=1
        before=self.authority.snapshot()
        with self.assertRaises(Fault):
            await self.daemon.maintain_once()
        self.assertEqual(self.authority.snapshot(),before)

    def test_scope_digest_preserves_physical_scope_and_binds_operator_mapping(self):
        physical=dict(service_id="physical",authority_id="id",site="site",
                      resources=["relay"],providers=["provider"],contracts=["relay:1"])
        self.assertEqual(scope_digest(physical),checkpoint_digest(physical))
        self.assertNotEqual(scope_digest(self.config),
            scope_digest(dict(self.config,controller_aliases=["different"])))
        self.assertNotEqual(scope_digest(self.config),
            scope_digest(dict(self.config,operator_actor_aliases={})))
        with self.assertRaises(ValueError):
            scope_digest(dict(self.config,authority_kind="unexpected"))


if __name__=="__main__":
    unittest.main()
