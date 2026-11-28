"""Publication failure must not expose commands, results or pairing state."""
import asyncio
import json
import os
import tempfile
import types
import unittest
from unittest.mock import patch

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
        self.daemon.witnessed_provider_polls={}
        self.daemon.witnessed_capability_demands={}
        self.daemon.witnessed_provider_statuses={}
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

    async def test_capability_demand_keeps_its_wire_type_binding_and_publication_fence(self):
        message = dict(type="capability_demand", id="observe", binding=B,
                       scope=dict(provider=self.actor, resource="slot", contract="job:1"))
        self.assertEqual("lease_demand", self.daemon.authority_request_band(message))
        reply = await self.daemon.dispatch(message)
        self.assertEqual("capability_demand_result", reply["type"])
        self.assertEqual({"lease": None}, reply["result"])
        self.assertIsNone(await self.daemon.recheck_witnessed_demand(message))
        self.daemon.remember_capability_demand(message, reply)
        before = self.published
        self.assertEqual(await self.daemon.recheck_witnessed_demand(message), reply)
        self.assertEqual(before, self.published)
        with patch.object(self.daemon.authority, "capability_demand",
                          return_value={"lease": {"id": "c"*64, "generation": 3}}):
            self.assertIsNone(await self.daemon.recheck_witnessed_demand(message))
        wrong = dict(message, binding=dict(B, grant_token="revoked"))
        good, stale = await self.daemon.dispatch_managed_reads([message, wrong])
        self.assertEqual(reply, good)
        self.assertEqual(409, stale["status"])
        self.assertNotIn("result", stale)
        self.current_phase = "prepared"
        denied = await self.daemon.dispatch(message)
        self.assertEqual(503, denied["status"])
        self.assertNotIn("result", denied)

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

    async def test_unchanged_witnessed_provider_poll_bypasses_publication_queue(self):
        message = self.message(request="provider-poll-request")
        envelope = message["envelope"]
        envelope.update(operation="provider.poll", arguments={"provider": "builtin"})
        envelope["auth"]["signature"] = sign(self.key, {
            key: value for key, value in envelope.items() if key != "auth"})
        self.assertIsNone(await self.daemon.recheck_witnessed_provider_poll(message))
        witnessed = await self.daemon.dispatch(message)
        self.assertEqual(witnessed["status"], 200)
        self.daemon.remember_provider_poll(message, witnessed)
        before = self.published
        self.assertEqual(witnessed, await self.daemon.recheck_witnessed_provider_poll(message))
        self.assertEqual(before, self.published)

        forged = dict(message, envelope=dict(envelope, auth={"actor": self.actor,
            "signature": self.message("unrelated")["envelope"]["auth"]["signature"]}))
        self.assertIsNone(await self.daemon.recheck_witnessed_provider_poll(forged))
        self.fail_validate = self.validated + 2
        self.assertIsNone(await self.daemon.recheck_witnessed_provider_poll(message))

        # A newly committed local activation has not passed the witness barrier.
        self.app.stores["site"].execute(lambda tx: tx.put("activations", "pending-fixture",
            {"id": "pending-fixture", "state": "pending", "provider": "builtin"}))
        self.assertIsNone(await self.daemon.recheck_witnessed_provider_poll(message))
        next_poll = await self.daemon.dispatch(message)
        self.daemon.remember_provider_poll(message, next_poll)
        self.assertEqual(next_poll, await self.daemon.recheck_witnessed_provider_poll(message))
        self.assertEqual(next_poll["result"]["activations"][0]["id"], "pending-fixture")

    def test_scoped_inbox_cache_isolates_generation_lease_and_actor(self):
        message = self.message()
        env = message["envelope"]
        env.update(operation="provider.poll", arguments={"provider": "builtin", "lease": "a"*64,
                                                         "generation": 1})
        first = self.daemon.provider_poll_key(message)
        self.assertIsNotNone(first)
        env["arguments"]["generation"] = 2
        self.assertNotEqual(first, self.daemon.provider_poll_key(message))
        env["arguments"]["generation"] = 1
        env["arguments"]["lease"] = "b"*64
        self.assertNotEqual(first, self.daemon.provider_poll_key(message))
        del env["arguments"]["lease"]
        self.assertIsNone(self.daemon.provider_poll_key(message))

    async def test_later_provider_poll_replies_while_another_command_waits_for_publication(self):
        poll = self.message(request="inbox-request")
        envelope = poll["envelope"]
        envelope.update(operation="provider.poll", arguments={"provider": "builtin"})
        envelope["auth"]["signature"] = sign(self.key, {
            key: value for key, value in envelope.items() if key != "auth"})
        poll["id"] = "first-poll"
        first, blocked, release, second = (asyncio.Event() for _ in range(4))
        seen = {}
        original = self.daemon.dispatch

        async def dispatch(message):
            if message["id"] == "blocked-command":
                blocked.set()
                await release.wait()
                return self.daemon.authority_response(message, B)
            return await original(message)

        class Socket:
            closed = False

            async def __aiter__(self):
                def frame(message):
                    return types.SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps(message))
                yield frame(poll)
                await first.wait()
                yield frame(dict(type="call", id="blocked-command", binding=B))
                await blocked.wait()
                yield frame(dict(poll, id="second-poll"))
                await release.wait()
                self.closed = True

            async def send_json(self, response):
                seen[response["id"]] = response
                if response["id"] == "first-poll":
                    first.set()
                if response["id"] == "second-poll":
                    second.set()

        self.daemon.dispatch = dispatch
        connection = asyncio.create_task(self.daemon.serve_controller_socket(Socket()))
        try:
            await asyncio.wait_for(second.wait(), 2)
            self.assertEqual(seen["second-poll"]["result"], seen["first-poll"]["result"])
            self.assertNotIn("blocked-command", seen)
        finally:
            release.set()
            await connection

    async def test_blocked_event_management_does_not_block_fresh_status_on_same_socket(self):
        entered, release, status_sent = asyncio.Event(), asyncio.Event(), asyncio.Event()
        replies = []

        async def dispatch(message):
            if message["id"] == "event":
                entered.set()
                await release.wait()
            return self.daemon.authority_response(message, B)

        class Socket:
            closed = False

            async def __aiter__(self):
                def frame(identifier, operation):
                    return types.SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps({"type": "call",
                        "id": identifier, "binding": B, "envelope": {"operation": operation}}))
                yield frame("event", "gateway.events.change")
                await entered.wait()
                yield frame("status", "cap.status")
                await release.wait()
                self.closed = True

            async def send_json(self, response):
                replies.append(response["id"])
                if response["id"] == "status":
                    status_sent.set()

        self.daemon.dispatch = dispatch
        connection = asyncio.create_task(self.daemon.serve_controller_socket(Socket()))
        try:
            await asyncio.wait_for(status_sent.wait(), 1)
            self.assertEqual(replies, ["status"])
        finally:
            release.set()
            await connection

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
                for message in ({"type": "registered", "rpc_limit": 2}, {"type": "call", "id": "first"}):
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

    async def test_expired_waiter_does_not_block_live_attachment_or_stop_running_effect(self):
        await self.assert_expired_waiter_reclaims_capacity("ordinary.call", 2)

    async def test_expired_event_waiter_does_not_block_attachment(self):
        await self.assert_expired_waiter_reclaims_capacity("gateway.events.change", 2)

    async def test_expired_event_waiter_is_skipped_before_dispatch_with_spare_capacity(self):
        await self.assert_expired_waiter_reclaims_capacity("gateway.events.change", 3)

    async def assert_expired_waiter_reclaims_capacity(self, operation, limit):
        started, release, done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        clock = [100.0]
        processed, replies = [], {}

        async def dispatch(message):
            processed.append(message["id"])
            if message["id"] == "running":
                started.set()
                await release.wait()
            return self.daemon.authority_response(message, B)

        class Socket:
            closed = False

            async def __aiter__(self):
                def frame(message):
                    return types.SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps(message))

                yield frame({"type": "registered", "rpc_limit": limit, "rpc_timeout_s": 5})
                yield frame({"type": "call", "id": "running", "envelope": {"operation": operation}})
                await started.wait()
                yield frame({"type": "call", "id": "expired", "envelope": {"operation": operation}})
                clock[0] += 6
                yield frame({"type": "call", "id": "live", "envelope": {"operation": "stream.attach"}})
                release.set()
                await done.wait()
                self.closed = True

            async def close(self):
                self.closed = True

            async def send_json(self, reply):
                replies[reply["id"]] = reply
                if {"running", "expired", "live"} <= replies.keys():
                    done.set()

        self.daemon.dispatch = dispatch
        with patch("biomerte.service_daemon.time", types.SimpleNamespace(monotonic=lambda: clock[0])):
            await asyncio.wait_for(self.daemon.serve_controller_socket(Socket()), 2)
        self.assertEqual(processed, ["running", "live"])
        self.assertEqual(replies["expired"]["status"], 503)
        self.assertEqual(replies["running"]["status"], 200)
        self.assertEqual(replies["live"]["status"], 200)

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

        async def dispatch_batch(messages):
            return [await dispatch(message) for message in messages]

        self.daemon.dispatch = dispatch
        self.daemon.dispatch_managed_reads = dispatch_batch
        await asyncio.wait_for(self.daemon.serve_controller_socket(Socket()), 1)
        self.assertEqual(processed, ["running", "diagnostic", "inspection", "resume", "lease-poll",
                                     "attachment", "lifecycle", "acquire-after-status",
                                     "registration", "confirm-installed-fence"])

    async def test_stream_lifecycle_backlog_leaves_admission_for_exact_lease_refresh(self):
        started, release, done = (asyncio.Event() for _ in range(3))
        replies = {}
        calls = [("busy", "cap.invoke"),
                 *((f"close-{n}", "stream.close") for n in range(6)),
                 ("inspect-lease", "cap.lease"), ("excess", "stream.close")]

        async def dispatch(message):
            if message["id"] == "busy":
                started.set()
                await release.wait()
            return self.daemon.authority_response(message, B)

        class Socket:
            closed = False

            async def __aiter__(self):
                yield types.SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps(
                    {"type": "registered", "rpc_limit": 8}))
                for request, operation in calls:
                    yield types.SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps({
                        "type": "call", "binding": B, "id": request,
                        "envelope": {"operation": operation, "arguments": {}}}))
                    if request == "busy":
                        await started.wait()
                release.set()
                await done.wait()
                self.closed = True

            async def send_json(self, response):
                replies[response["id"]] = response["status"]
                if len(replies) == len(calls):
                    done.set()

        self.daemon.dispatch = dispatch
        await asyncio.wait_for(self.daemon.serve_controller_socket(Socket()), 2)
        self.assertEqual(replies.pop("excess"), 429)
        self.assertEqual(set(replies.values()), {200})

    async def run_socket_burst(self, calls):
        started, release, completed = (asyncio.Event() for _ in range(3))
        replies, publications = {}, []

        async def dispatch(message):
            publications.append([message["id"]])
            if message["id"] == "running":
                started.set()
                await release.wait()
            return self.daemon.authority_response(message, B)

        async def dispatch_batch(messages):
            publications.append([message["id"] for message in messages])
            return [self.daemon.authority_response(message, B) for message in messages]

        class Socket:
            closed = False

            async def __aiter__(self):
                def wire(message):
                    return types.SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps(message))
                yield wire({"type": "registered", "rpc_limit": 64})
                for request, operation in calls:
                    yield wire({"type": "call", "id": request, "binding": B,
                                "envelope": {"operation": operation}})
                    if request == "running":
                        await started.wait()
                release.set()
                await completed.wait()
                self.closed = True

            async def send_json(self, response):
                replies[response["id"]] = response["status"]
                if len(replies) == len(calls):
                    completed.set()

        self.daemon.dispatch = dispatch
        self.daemon.dispatch_managed_reads = dispatch_batch
        await asyncio.wait_for(self.daemon.serve_controller_socket(Socket()), 2)
        return replies, publications

    def general_burst(self, size):
        return [("running", "cap.invoke"),
                *((f"waiting-{n}", "cap.invoke") for n in range(size - 1))]

    async def test_full_gateway_window_settles_mixed_provider_reads_and_signed_submissions(self):
        calls = [("running", "cap.invoke")]
        calls.extend((f"inbox-{i}", "provider.poll") for i in range(40))
        calls.extend((f"submit-{i}", "cap.submit") for i in range(16))
        calls.extend((name, op) for name, op in (("historical", "cap.status"), ("lease", "cap.lease"),
                     ("progress", "provider.result"), ("discover", "system.discovery"),
                     ("events", "gateway.events.grok"), ("fence", "cap.fenced"), ("open", "stream.open")))
        self.assertEqual(len(calls), 64)
        replies, batches = await self.run_socket_burst([*calls, ("over-limit", "cap.submit")])
        self.assertEqual(replies.pop("over-limit"), 429)
        self.assertEqual(set(replies.values()), {200})
        for name, _ in calls:
            self.assertEqual(sum(batch.count(name) for batch in batches), 1)

    async def test_discovery_and_grok_are_admitted_with_a_full_general_queue(self):
        calls = self.general_burst(61) + [
            ("poll", "provider.poll"), ("catalog", "system.discovery"),
            ("grok-list", "gateway.events.grok"), ("excess", "cap.invoke")]
        replies, _ = await self.run_socket_burst(calls)
        self.assertEqual(replies.pop("excess"), 429)
        self.assertEqual(set(replies.values()), {200})

    async def test_provider_can_advance_a_lease_with_the_general_queue_full(self):
        calls = self.general_burst(59) + [
            ("submit", "cap.submit"), ("claim", "provider.claim"),
            ("heartbeat", "cap.heartbeat"), ("result", "provider.result"),
            ("reconcile", "provider.reconcile"), ("excess", "cap.invoke")]
        replies, batches = await self.run_socket_burst(calls)
        self.assertEqual(replies.pop("excess"), 429)
        self.assertEqual(set(replies.values()), {200})
        self.assertIn(["submit", "claim", "heartbeat", "result", "reconcile"], batches)

    async def test_nonbatchable_bands_keep_each_request_and_their_fifo_order(self):
        operations = ("cap.invoke", "stream.open", "stream.close")
        calls = [("running", "cap.invoke")]
        calls.extend((f"{operation}-{i}", operation) for i in range(7) for operation in operations)
        calls.append(("progress", "provider.result"))
        replies, publications = await self.run_socket_burst(calls)
        self.assertEqual(set(replies.values()), {200})
        self.assertEqual(len(publications), len(calls))
        for operation in operations:
            expected = [request for request, op in calls if op == operation]
            observed = [batch[0] for batch in publications if batch[0] in expected]
            self.assertEqual(observed, expected)

    async def queued_publications(self, operations):
        running, release, done = (asyncio.Event() for _ in range(3))
        published, replies = [], []
        async def dispatch(message):
            if message["id"] == "cap.invoke":
                running.set()
                await release.wait()
            published.append([message["id"]])
            return self.daemon.authority_response(message, B)

        async def dispatch_batch(messages):
            published.append([message["id"] for message in messages])
            return [self.daemon.authority_response(message, B) for message in messages]

        class Socket:
            closed = False

            async def __aiter__(self):
                for operation in operations:
                    yield types.SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps({
                        "type": "call", "id": operation, "binding": B,
                        "envelope": {"operation": operation, "arguments": {}}}))
                    if operation == "cap.invoke":
                        await running.wait()
                release.set()
                await done.wait()
                self.closed = True

            async def send_json(self, reply):
                replies.append(reply["id"])
                if len(replies) == len(operations):
                    done.set()

        self.daemon.dispatch = dispatch
        self.daemon.dispatch_managed_reads = dispatch_batch
        await asyncio.wait_for(self.daemon.serve_controller_socket(Socket()), 2)
        self.assertCountEqual(replies, operations)
        return published

    async def test_cold_stream_and_lease_work_share_the_next_publication_under_mixed_load(self):
        operations = ["cap.invoke", "cap.status", "provider.poll", "cap.submit", "provider.claim",
                      "stream.inspect", "stream.attach", "stream.resume", "cap.lease"]

        published = await self.queued_publications(operations)
        self.assertTrue({"cap.status", "cap.submit", "provider.claim", "stream.inspect",
                         "stream.attach", "stream.resume", "cap.lease"}.issubset(published[1]), published)

    async def test_health_inbox_and_discovery_share_a_due_lease_publication(self):
        operations = ["cap.invoke", "cap.status", "provider.poll", "cap.submit", "provider.claim",
                      "system.health", "system.discovery", "cap.lease"]
        published = await self.queued_publications(operations)
        self.assertCountEqual(published[1], operations[1:])
        self.assertEqual(len(published), 2, published)

    async def test_sixteen_signed_submissions_settle_with_exact_receipts_under_concurrent_load(self):
        first, release, done = (asyncio.Event() for _ in range(3))
        replies = {}
        dispatch = self.daemon.dispatch

        def frame(message):
            return types.SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps(message))

        def signed_echo(index):
            key = "parallel-echo-" + str(index)
            message = self.message(request=key)
            message["id"] = key
            envelope = message["envelope"]
            envelope.update(operation="cap.submit", arguments={"contract": "core.echo:1",
                            "inputs": {"value": index}, "operation": key})
            envelope["auth"]["signature"] = sign(self.key, {
                name: value for name, value in envelope.items() if name != "auth"})
            return message

        messages = [signed_echo(i) for i in range(16)]
        repeated = dict(messages[0], id="retry-same-signed-envelope")
        forged = json.loads(json.dumps(messages[0]))
        forged["id"] = "unauthorized-copy"
        forged["envelope"]["auth"]["signature"] = messages[1]["envelope"]["auth"]["signature"]
        wanted = [*messages, repeated, forged]

        async def suspended(message):
            if message["id"] == "earlier":
                first.set()
                await release.wait()
                return self.daemon.authority_response(message, B)
            return await dispatch(message)

        class Socket:
            closed = False

            async def __aiter__(self):
                yield frame({"type": "registered", "rpc_limit": 64, "rpc_timeout_s": 20})
                yield frame({"type": "call", "id": "earlier", "binding": B})
                await first.wait()
                for message in wanted:
                    yield frame(message)
                    await asyncio.sleep(0)
                release.set()
                await done.wait()
                self.closed = True

            async def send_json(self, reply):
                replies[reply["id"]] = reply
                if len(replies) == len(wanted) + 1:
                    done.set()

        self.daemon.dispatch = suspended
        await asyncio.wait_for(self.daemon.serve_controller_socket(Socket()), 15)
        for i, message in enumerate(messages):
            reply = replies[message["id"]]
            self.assertEqual(reply["status"], 200, reply)
            self.assertEqual(reply["result"]["result"], {"value": i})
        self.assertEqual(replies[repeated["id"]]["result"], replies[messages[0]["id"]]["result"])
        self.assertNotEqual(replies[forged["id"]]["status"], 200)
        self.assertNotIn("result", replies[forged["id"]])
        self.assertEqual(self.published, 2)

    async def test_admission_services_recovery_and_an_aging_submission_at_capacity(self):
        first, release, done = (asyncio.Event() for _ in range(3))
        sent, batches = {}, []

        async def dispatch(message):
            if message["id"] == "earlier":
                first.set()
                await release.wait()
            batches.append([message["id"]])
            return self.daemon.authority_response(message, B)

        async def dispatch_batch(messages):
            batches.append([message["id"] for message in messages])
            return [self.daemon.authority_response(message, B) for message in messages]

        class Socket:
            closed = False

            async def __aiter__(self):
                def wire(message):
                    return types.SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps(message))
                def call(name, operation):
                    return wire({"type": "call", "id": name, "binding": B,
                                 "envelope": {"operation": operation}})
                yield wire({"type": "registered", "rpc_limit": 9, "rpc_timeout_s": 0.02})
                yield call("earlier", "cap.invoke")
                await first.wait()
                for i in range(4):
                    yield call("submit" + str(i), "cap.submit")
                yield call("open", "stream.open")
                yield call("submit4", "cap.submit")
                yield call("status", "cap.status")
                yield call("claim", "provider.claim")
                yield call("excess", "cap.submit")
                await asyncio.sleep(0.015)
                release.set()
                await done.wait()
                self.closed = True

            async def send_json(self, reply):
                sent[reply["id"]] = reply["status"]
                if len(sent) == 10:
                    done.set()

        self.daemon.dispatch, self.daemon.dispatch_managed_reads = dispatch, dispatch_batch
        await asyncio.wait_for(self.daemon.serve_controller_socket(Socket()), 2)
        self.assertEqual(sent.pop("excess"), 429)
        self.assertEqual(set(sent.values()), {200})
        self.assertEqual(batches[1][:4], ["submit0", "submit1", "submit2", "submit3"])
        self.assertIn("claim", batches[1])
        self.assertIn("status", batches[1])
        self.assertIn(["open"], batches)

    async def test_completed_result_is_admitted_with_the_provider_inbox_queue_full(self):
        started, release, done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        replies = {}

        def call(rpc, operation, arguments):
            message = self.message(request=rpc + "-request")
            message["id"] = rpc
            envelope = message["envelope"]
            envelope.update(operation=operation, arguments=arguments)
            envelope["auth"]["signature"] = sign(self.key, {
                key: value for key, value in envelope.items() if key != "auth"})
            return message

        submission = call("submit", "cap.submit", {"contract": "core.echo:1",
            "operation": "completed-echo", "inputs": {"value": "verified"}})
        self.assertEqual((await self.daemon.dispatch(submission))["status"], 200)
        dispatch = self.daemon.dispatch

        async def suspend_first(message):
            if message["id"] == "unrelated":
                started.set()
                await release.wait()
                return self.daemon.authority_response(message, B)
            return await dispatch(message)

        # The deployed reader receives already signed envelopes. Generate load
        # before measuring admission; OpenSSL subprocess startup is client work.
        inbox_messages = [call("inbox-" + str(n), "provider.poll", {"provider": "builtin"})
                          for n in range(15)]
        result_read = call("read-result", "cap.status", {"operation": "completed-echo"})

        class Socket:
            closed = False

            async def __aiter__(self):
                def frame(message):
                    return types.SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps(message))
                yield frame({"type": "registered", "rpc_limit": 16})
                yield frame({"type": "call", "id": "unrelated", "binding": B})
                await started.wait()
                for message in inbox_messages[:-1]:
                    yield frame(message)
                yield frame(result_read)
                yield frame(inbox_messages[-1])
                release.set()
                await done.wait()
                self.closed = True

            async def close(self):
                self.closed = True

            async def send_json(self, reply):
                replies[reply["id"]] = reply
                if reply["id"] == "read-result":
                    done.set()

        self.daemon.dispatch = suspend_first
        connection = asyncio.create_task(self.daemon.serve_controller_socket(Socket()))
        try:
            await asyncio.wait_for(done.wait(), 2)
        finally:
            release.set()
            done.set()
            await connection
        self.assertEqual(replies["inbox-14"]["status"], 429)
        self.assertEqual(replies["read-result"]["status"], 200)
        self.assertEqual(replies["read-result"]["result"]["result"], {"value": "verified"})

    async def test_claimed_status_skips_an_unrelated_publication_only_while_unchanged(self):
        operation = "already-claimed"
        action = dict(id=operation, provider="test-provider", lease="active-lease", state="claimed")
        self.app.stores["site"].execute(lambda tx: tx.put("actions", operation, action))
        message = self.message(request="inspect-claimed")
        envelope = message["envelope"]
        envelope.update(operation="cap.status", arguments={"operation": operation})
        envelope["auth"]["signature"] = sign(self.key, {
            key: value for key, value in envelope.items() if key != "auth"})
        self.assertIsNone(await self.daemon.recheck_witnessed_provider_status(message))
        reply = await self.daemon.dispatch(message)
        self.assertEqual(reply["status"], 200)
        self.daemon.remember_provider_status(message, reply)
        async with self.daemon.lock:
            self.assertEqual(await self.daemon.recheck_witnessed_provider_status(message), reply)
        self.app.stores["site"].execute(lambda tx: tx.put("actions", operation,
            dict(action, state="succeeded", result={"done": True})))
        self.assertIsNone(await self.daemon.recheck_witnessed_provider_status(message))

    async def test_completed_result_is_delivered_while_another_command_waits_for_publication(self):
        def call(rpc, operation, arguments):
            message = self.message(request=rpc + "-request")
            message["id"] = rpc
            envelope = message["envelope"]
            envelope.update(operation=operation, arguments=arguments)
            envelope["auth"]["signature"] = sign(self.key, {
                key: value for key, value in envelope.items() if key != "auth"})
            return message

        submission = call("submit", "cap.submit", {"contract": "core.echo:1",
            "operation": "completed-echo", "inputs": {"value": "verified"}})
        result = call("read-result", "cap.status", {"operation": "completed-echo"})
        submitted, blocked, release, done = (asyncio.Event() for _ in range(4))
        replies = {}
        dispatch = self.daemon.dispatch

        async def suspend_first(message):
            if message["id"] == "unrelated":
                blocked.set()
                await release.wait()
                return self.daemon.authority_response(message, B)
            return await dispatch(message)

        class Socket:
            closed = False

            async def __aiter__(self):
                def frame(message):
                    return types.SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps(message))
                yield frame(submission)
                await submitted.wait()
                yield frame({"type": "call", "id": "unrelated", "binding": B})
                await blocked.wait()
                yield frame(result)
                await release.wait()
                self.closed = True

            async def send_json(self, reply):
                replies[reply["id"]] = reply
                if reply["id"] == "submit":
                    submitted.set()
                elif reply["id"] == "read-result":
                    done.set()

        self.daemon.dispatch = suspend_first
        connection = asyncio.create_task(self.daemon.serve_controller_socket(Socket()))
        try:
            await asyncio.wait_for(done.wait(), 2)
            self.assertEqual(replies["read-result"]["result"]["result"], {"value": "verified"})
            self.assertNotIn("unrelated", replies)
        finally:
            release.set()
            await connection

        forged = dict(result, envelope=dict(result["envelope"], auth=submission["envelope"]["auth"]))
        self.assertIsNone(await self.daemon.recheck_witnessed_provider_status(forged))
        self.fail_validate = self.validated + 2
        self.assertIsNone(await self.daemon.recheck_witnessed_provider_status(result))

        def retire(tx):
            action = tx.need("actions", "completed-echo")
            action.pop("result")
            action["result_expired"] = True
            tx.put("actions", "completed-echo", action)

        self.app.stores["site"].execute(retire)
        self.assertIsNone(await self.daemon.recheck_witnessed_provider_status(result))

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
