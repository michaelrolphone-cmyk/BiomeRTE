"""Cross-repository TLS/WS integration with real helpers and scoped authorities.

Set GATEWAY_SOURCE to the companion BiomeRTE-Heorku-Gateway-Stream checkout.
An adjacent checkout with that repository name is also detected. The default
local suite skips these cases when the companion checkout is absent. Setting
GATEWAY_SOURCE makes the companion protocol tests mandatory.
Both HTTP and WebSocket RPCs run over a local TLS server; native signatures, checkpoint
storage, preparation, fencing, and activation use their real implementations.
Only the physical provider is a generated-key fixture.
"""
import asyncio
import contextlib
import importlib
import json
import os
import pathlib
import shutil
import ssl
import subprocess
import sys
import tempfile
import types
import unittest
import uuid
from unittest.mock import AsyncMock, Mock, patch

try:
    from aiohttp import ClientSession, ClientConnectorCertificateError, TCPConnector, web
    from aiohttp.test_utils import TestServer
except ModuleNotFoundError as error:
    if os.environ.get("GATEWAY_SOURCE"):
        raise
    raise unittest.SkipTest(
        'Optional companion integration requires aiohttp; install the companion '
        'gateway requirements and set GATEWAY_SOURCE to run it') from error

from biomerte.app import Application, initialize
from biomerte.api import Context
from biomerte import providers
from biomerte.identity import generate, fingerprint, sign
from biomerte.service_authority import ScopedAuthority
from biomerte.service_checkpoint import ADAPTER_PROTOCOL, CheckpointError, CheckpointStore, checkpoint_ref
from biomerte.service_daemon import CheckpointHelper, AuthorityDaemon, local_rpc, scope_digest
from biomerte.store import Fault


SECRET = "gateway-fixture-secret-" + "s" * 40
SERVICE = "relay-authority"
RESOURCE = "relay.integration"
CONTRACT = "relay.integration:1"


class Clock:
    def __init__(self):
        self.now = 0.0
    def __call__(self):
        return self.now
    def advance(self, seconds):
        self.now += seconds


class ControllerPublicationTest(unittest.IsolatedAsyncioTestCase):
    async def test_slow_signed_batch_attributes_publication_time_without_exposing_envelopes(self):
        clock = Clock()
        daemon = AuthorityDaemon.__new__(AuthorityDaemon)
        daemon.binding = {"boot_epoch": "private_binding"}
        daemon.config = {"authority_kind": "controller"}
        daemon.lock = asyncio.Lock()
        daemon.phase = AsyncMock(return_value={"phase": "active"})
        daemon.authority = Mock()
        daemon.authority.call.return_value = {"value": "private_result"}
        daemon.transport = Mock(validate=AsyncMock())
        durations = iter((0.25, 4.5))

        async def publish():
            clock.advance(next(durations))

        daemon.publish = publish
        messages = [{"id": str(i), "type": "call", "binding": daemon.binding,
                     "envelope": {"operation": "cap.status", "id": "private_request"}}
                    for i in range(2)]
        with patch("biomerte.service_daemon.time", Mock(monotonic=clock)), \
                self.assertLogs("biomerte.service", level="WARNING") as logs:
            replies = await daemon.dispatch_managed_reads(messages)
        self.assertEqual([r["result"] for r in replies], [{"value": "private_result"}] * 2)
        log, = logs.output
        self.assertIn("activity=batch count=2 seconds=4.750", log)
        self.assertIn("prior_publish=0.250", log)
        self.assertIn("result_publish=4.500", log)
        self.assertNotIn("private_", log)

    async def test_start_fences_checks_exact_controller_grant_once_per_attempt(self):
        grant = dict(service_id="controller", manifest_revision="revision", worker_id="mac",
                     worker_incarnation="incarnation", boot_epoch="epoch", grant_token="token")
        coordinator = Mock(validate=AsyncMock())
        helper = CheckpointHelper.__new__(CheckpointHelper)
        helper.config = dict(authority_kind="controller", service_id="controller", worker_id="mac")
        helper.transport, helper.token, helper.replication = coordinator, "helper-token", "complete"
        helper.store = Mock()
        helper.store.phase.return_value = dict(binding=grant, phase="prepared", checkpoint="root")
        helper.store.generation_floors.return_value = {}
        app = web.Application()
        app.router.add_post("/adapter", helper.handle)
        async with TestServer(app) as server, ClientSession() as session:
            daemon = AuthorityDaemon.__new__(AuthorityDaemon)
            daemon.config, daemon.session, daemon.binding = helper.config, session, grant
            daemon.url, daemon.token = str(server.make_url("/adapter")), helper.token
            daemon.transport, daemon.lock = coordinator, asyncio.Lock()
            daemon.authority, daemon.publish = Mock(), AsyncMock()
            with patch.dict(os.environ, BIOME_SERVICE_CHECKPOINT="root"):
                await daemon.start_fences()
                coordinator.validate.assert_awaited_once_with(grant)
                daemon.authority.prepare_fences.assert_called_once_with("root", {})
                daemon.publish.assert_awaited_once()

                for expected_status in (503, 409):
                    with self.subTest(expected_status=expected_status):
                        coordinator.validate.reset_mock()
                        coordinator.validate.side_effect = (Fault("Revoked grant", 503)
                                                            if expected_status == 503 else None)
                        helper.store.phase.return_value = dict(binding=dict(grant, boot_epoch="old"))
                        with self.assertRaises(Fault) as failure:
                            await daemon.start_fences()
                        self.assertEqual(failure.exception.status, expected_status)
                        coordinator.validate.assert_awaited_once_with(grant)
                        self.assertEqual(daemon.authority.prepare_fences.call_count, 1)
                        self.assertEqual(daemon.publish.await_count, 1)

    async def test_maintenance_reports_the_failing_stage_and_status_without_data(self):
        daemon = AuthorityDaemon.__new__(AuthorityDaemon)
        daemon.config, daemon.binding = {}, {"test": "binding"}
        daemon.lock = asyncio.Lock()
        daemon.transport = Mock(validate=AsyncMock())
        daemon.phase = AsyncMock(return_value={"phase": "active"})
        daemon.publish = AsyncMock()
        daemon.authority = Mock()
        daemon.events = Mock()
        daemon.events.deliver.side_effect = Fault("private callback payload", 409)
        with self.assertLogs("biomerte.service", level="WARNING") as logs, \
             patch("biomerte.service_daemon.asyncio.sleep", side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await daemon.maintenance_loop()
        self.assertIn("stage=event_delivery status=409", " ".join(logs.output))
        self.assertNotIn("private callback payload", " ".join(logs.output))

    async def test_live_publication_without_standby_avoids_full_status_probe(self):
        helper = CheckpointHelper.__new__(CheckpointHelper)
        helper.config = {"authority_kind": "controller", "worker_id": "mac",
                         "replica_workers": ["mac", "pi"]}
        binding = {"boot_epoch": "boot"}
        packet = {"payload": {"service_id": "controller", "sequence": 5, "state": {}},
                  "digest": "a" * 64}
        ref = checkpoint_ref(packet)
        reply = {"binding": binding, "checkpoint": ref,
                 "head": dict(ref, boot_epoch="boot"), "acknowledgements": []}
        helper.witness_reference_epoch = None
        helper.replica_blocks = {}
        helper.validate = AsyncMock()
        helper.workers = AsyncMock(return_value=({"boot_epoch": "boot"}, {"mac": {"worker_id": "mac"}}))
        helper.status = AsyncMock(side_effect=AssertionError("No standby to inspect"))
        helper.replicate_chunks = AsyncMock()
        helper.transport = types.SimpleNamespace(request=AsyncMock(return_value=reply))
        helper.store = types.SimpleNamespace(published=Mock())

        self.assertEqual(reply, await helper.publish_packet(binding, packet))
        helper.status.assert_not_awaited()
        helper.transport.request.assert_awaited_once_with("/api/service-checkpoints/replicate", {
            "binding": binding, "local_durable": True,
            "checkpoint": dict(ref, package=packet), "targets": []})
        helper.store.published.assert_called_once_with(ref)
        self.assertEqual("degraded", helper.replication)

    async def test_compact_publication_negotiates_then_reverts_to_full_for_returning_peer(self):
        helper = CheckpointHelper.__new__(CheckpointHelper)
        helper.config = {"authority_kind": "controller", "worker_id": "mac",
                         "replica_workers": ["mac", "pi"]}
        helper.witness_reference_epoch = None
        helper.replica_blocks = {}
        helper.validate = AsyncMock()
        helper.store = types.SimpleNamespace(published=Mock())
        helper.replicate_chunks = AsyncMock()
        binding = {"boot_epoch":"boot", "grant_token":"grant"}
        mac = dict(worker_id="mac", worker_incarnation="one")
        pi = dict(worker_id="pi", worker_incarnation="two")
        owner = {"mac": mac}
        both = dict(owner, pi=pi)
        helper.publication_workers = AsyncMock(return_value=owner)
        packet = {"payload": {"service_id": "controller", "scope_digest":"c"*64,
                  "sequence":5, "previous_digest":"d"*64, "state":{"opaque":"x"*933000}},
                  "digest":"a"*64}
        ref = checkpoint_ref(packet)
        response = {"binding":binding, "checkpoint":ref, "head":dict(ref, boot_epoch="boot"),
                    "acknowledgements":[], "owner_only_witness_reference":True}
        request = AsyncMock(return_value=response)
        helper.transport = types.SimpleNamespace(request=request)

        await helper.publish_packet(binding, packet)  # Negotiate with a full packet.
        self.assertIs(packet, request.await_args.args[1]["checkpoint"]["package"])
        await helper.publish_packet(binding, packet)
        compact = request.await_args.args[1]
        self.assertEqual(ref, compact["checkpoint"])
        self.assertEqual({"scope_digest":"c"*64, "previous_digest":"d"*64}, compact["witness_reference"])
        self.assertLess(len(json.dumps(compact)), 1024)
        helper.replicate_chunks.assert_not_awaited()

        helper.publication_workers.side_effect = [owner, both]
        response_with_peer = dict(response, acknowledgements=[dict(pi, checkpoint=ref, durable=True)])
        request.side_effect = [Fault("Full publication required", 409), response_with_peer]
        await helper.publish_packet(binding, packet)
        full = request.await_args.args[1]
        self.assertIs(packet, full["checkpoint"]["package"])
        self.assertEqual([pi], full["targets"])
        self.assertNotIn("witness_reference", full)
        helper.replicate_chunks.assert_awaited_once_with(binding, packet, [pi])

        # A new boot and an old gateway's reply disable compact publication.
        binding = dict(binding, boot_epoch="other")
        request.side_effect = None
        request.return_value = {**response, "head":dict(ref, boot_epoch="other"), "binding":binding}
        del request.return_value["owner_only_witness_reference"]
        for _ in range(2):
            await helper.publish_packet(binding, packet, owner)
            self.assertIs(packet, request.await_args.args[1]["checkpoint"]["package"])

    async def test_unchanged_root_reuses_ack_but_failed_dirty_publication_retries(self):
        daemon = AuthorityDaemon.__new__(AuthorityDaemon)
        daemon.config = {"authority_kind": "controller"}
        daemon.confirmed_controller_publication = None
        daemon.controller_publication_refresh_at = 0
        dirty = True
        daemon.authority = types.SimpleNamespace(checkpoint_is_dirty=lambda: dirty)

        async def witness(instance):
            nonlocal dirty
            dirty = False
            if response == "lost":
                raise Fault("Lost publication acknowledgement", 503)
            return {"checkpoint": response}

        response = "initial"
        with patch("biomerte.controller_checkpoint.publish_controller", side_effect=witness) as publish:
            self.assertEqual({"checkpoint": "initial"}, await daemon.publish())
            self.assertEqual({"checkpoint": "initial"}, await daemon.publish())
            publish.assert_awaited_once_with(daemon)
            dirty = True
            response = "lost"
            with self.assertRaises(Fault):
                await daemon.publish()
            self.assertFalse(dirty)
            response = "reconciled"
            self.assertEqual({"checkpoint": "reconciled"}, await daemon.publish())
            self.assertEqual(3, publish.await_count)
            daemon.controller_publication_refresh_at = 0
            await daemon.publish()
            self.assertEqual(4, publish.await_count)


class DaemonIntegration(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        path = pathlib.Path(os.environ.get("GATEWAY_SOURCE") or
            pathlib.Path(__file__).resolve().parents[2] / "BiomeRTE-Heorku-Gateway-Stream")
        required = ("service_coordinator.py", "checkpoint_transport.py", "service_transport.py")
        if any(not (path / "biomerte" / name).is_file() for name in required):
            if os.environ.get("GATEWAY_SOURCE"):
                raise FileNotFoundError(f"GATEWAY_SOURCE lacks the required gateway modules: {path}")
            raise unittest.SkipTest(
                "Optional companion integration: set GATEWAY_SOURCE to the tested "
                "BiomeRTE-Heorku-Gateway-Stream checkout, or place it beside BiomeRTE")
        package = types.ModuleType("_service_gateway_integration")
        package.__path__ = [str(pathlib.Path(path) / "biomerte")]
        sys.modules[package.__name__] = package
        cls.coordinator_module = importlib.import_module(package.__name__ + ".service_coordinator")
        cls.workers_module = importlib.import_module(package.__name__ + ".checkpoint_transport")
        cls.authorities_module = importlib.import_module(package.__name__ + ".service_transport")
        cls.keys = tempfile.TemporaryDirectory()
        cls.key_root = pathlib.Path(cls.keys.name)
        cls.private = cls.key_root / "private"
        cls.private.mkdir()
        cls.identity = initialize(str(cls.private), "integration-site")
        private_config = json.loads((cls.private / "node.json").read_text())
        private_config.update(stream_router_secret="fixture-stream-secret-" + "t" * 48,
                              stream_gateway_url="wss://fixture.invalid/stream")
        (cls.private / "node.json").write_text(json.dumps(private_config))
        cls.provider_key = str(cls.key_root / "provider.key")
        cls.provider_public = generate(cls.provider_key)
        cls.provider_actor = fingerprint(cls.provider_public)
        cls.consumer_key = str(cls.key_root / "consumer.key")
        cls.consumer_public = generate(cls.consumer_key)
        cls.consumer_actor = fingerprint(cls.consumer_public)
        cls.cert = cls.key_root / "server.crt"
        cls.tls_key = cls.key_root / "server.key"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-subj", "/CN=localhost", "-days", "1",
                        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
                        "-keyout", str(cls.tls_key), "-out", str(cls.cert)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.server_tls.load_cert_chain(str(cls.cert), str(cls.tls_key))

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "keys"):
            cls.keys.cleanup()

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = pathlib.Path(self.directory.name)
        self.tasks, self.runners, self.gateway_objects = [], [], []
        self.helpers, self.configs, self.helper_tasks = {}, {}, {}
        self.daemons, self.authority_tasks, self.closed_apps = {}, {}, set()
        self.addAsyncCleanup(self.cleanup)
        # This fixture trusts only its generated local TLS listener. Production
        # Transport still requires HTTPS and uses its normal verified session.
        self.client = ClientSession(connector=TCPConnector(ssl=False))
        self.clock = Clock()
        self.spec = dict(id=SERVICE, images={"linux/arm64": "sha256:" + "a" * 64},
            effect_policy="externally_fenced", state_policy="externally_managed",
            activation={"protocol": ADAPTER_PROTOCOL, "binding": "relay-authority"},
            authority={"id": self.identity["node"], "resources": [RESOURCE],
                       "providers": [self.provider_actor]},
            lease_ttl_s=5, worker_ttl_s=10, startup_grace_s=20,
            preferred_workers=["pi", "mac"])
        self.spec["revision"] = self.coordinator_module.manifest_revision(self.spec)
        await self.start_gateway()
        self.clock.advance(5)
        self.grant = self.heartbeat("pi")["assignments"][0]
        self.heartbeat("mac")
        self.binding = self.coordinator_module.grant_binding(self.grant)
        self.common = dict(service_id=SERVICE, authority_id=self.identity["node"],
            site="integration-site", resources=[RESOURCE], providers=[self.provider_actor],
            contracts=[CONTRACT], fence_contracts={RESOURCE: CONTRACT},
            replica_workers=["pi", "mac"], private_dir=str(self.private),
            coordinator_origin=self.origin, request_timeout_s=3)
        for name, value in (("coordinator", self.coordinator.key),
                            ("helper", "h" * 48), ("checkpoint", "k" * 48)):
            path = self.root / (name + ".key")
            path.write_text(value)
            self.common[name + "_key_file"] = str(path)
        seed_home = self.root / "seed"
        seed_home.mkdir()
        for name in ("node.json", "node.key"):
            shutil.copy2(self.private / name, seed_home / name)
        seed = Application(str(seed_home))
        def populate(tx):
            context = Context(seed, scope="site")
            providers.define(tx, context, "relay.integration", {}, {}, version=1)
            providers.offer(tx, context, self.provider_actor, CONTRACT, RESOURCE,
                            configuration={"stream": {"enabled": True, "lanes": ["control"],
                                "direction": "bidirectional"}},
                            capacity=1, lease_class="persistent")
            tx.put("peers", self.provider_actor, dict(id=self.provider_actor,
                key=self.provider_public, enabled=True,
                scopes=["cap.offer", "provider.poll", "cap.fenced", "provider.result",
                        "cap.status", "stream.attach", "offer:" + CONTRACT, "resource:" + RESOURCE]))
            tx.put("peers", self.consumer_actor, dict(id=self.consumer_actor,
                key=self.consumer_public, enabled=True, scopes=[
                    "cap.acquire", "cap.lease", "cap.release", "cap.status",
                    "stream.open", "stream.attach", "stream.inspect", "stream.list", "stream.close",
                    "use:" + CONTRACT, "contract:" + CONTRACT, "resource:" + RESOURCE]))
            tx.put("_fence_generations", RESOURCE, 183)
            tx.put("guards", RESOURCE, dict(holder="", generation=184, resource=RESOURCE))
        seed.stores["site"].execute(populate)
        state = ScopedAuthority(seed, self.common, self.binding).snapshot()
        for store in seed.stores.values():
            store.close()
        genesis_store = CheckpointStore(str(self.root / "genesis.sqlite"), SERVICE,
            scope_digest(self.common), ("k" * 48).encode())
        packet = genesis_store.bootstrap(state, {"source_receipt": "isolated-signed-fixture"})
        genesis_store.close()
        self.genesis = packet
        bootstrap = self.root / "bootstrap.json"
        bootstrap.write_text(json.dumps(packet))
        for worker in ("pi", "mac"):
            config = dict(self.common, worker_id=worker,
                checkpoint_db=str(self.root / worker / "checkpoint.sqlite"),
                authority_home=str(self.root / worker / "authority"),
                bootstrap_packet=str(bootstrap), child_url_template="http://127.0.0.1:1")
            helper = CheckpointHelper(config, self.client)
            app = web.Application()
            app.router.add_get("/health", helper.health)
            app.router.add_post("/adapter", helper.handle)
            app.router.add_post("/fence", helper.handle)
            base, unused = await self.server(app)
            config["adapter_url"] = base + "/adapter"
            self.helpers[worker], self.configs[worker] = helper, config
            self.start_helper(worker)
        await self.eventually(lambda: len(self.workers.links) == 2,
                              "both independent helpers must register")

    async def server(self, app, tls=False):
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        self.runners.append(runner)
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=self.server_tls if tls else None)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return ("https" if tls else "http") + "://127.0.0.1:" + str(port), runner

    async def start_gateway(self):
        self.coordinator = self.coordinator_module.Coordinator(
            SECRET, max_lease_ttl_s=5, clock=self.clock)
        self.workers = self.workers_module.CheckpointWorkers(self.coordinator, timeout=1)
        self.services = self.authorities_module.ServiceAuthorities(self.coordinator, SECRET, timeout=2)
        self.gateway_objects.extend((self.services, self.workers))
        app = web.Application()
        self.coordinator.mount(app)
        self.workers.mount(app)
        self.services.mount(app)
        self.origin, self.gateway_runner = await self.server(app, tls=True)

    def heartbeat(self, worker, grant=None, proof=None, fence=None, incarnation=None, healthy=None):
        row = dict(manifest=self.spec, manifest_revision=self.spec["revision"],
            platform="linux/arm64", image_ref=self.spec["images"]["linux/arm64"],
            prepared=True, dependencies_ready=True, healthy=healthy)
        if grant:
            row.update(boot_epoch=grant["boot_epoch"], grant_token=grant["grant_token"])
        if proof:
            row.update(state_ready=True, state_proof=proof)
        if fence:
            row["fence_ack"] = fence
        return self.coordinator.heartbeat(dict(worker_id=worker,
            worker_incarnation=incarnation or worker + "-1", services=[row]))

    def start_helper(self, worker):
        task = asyncio.create_task(self.helpers[worker].worker_loop())
        self.tasks.append(task)
        self.helper_tasks[worker] = task

    async def stop_helper(self, worker):
        task = self.helper_tasks.pop(worker, None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.eventually(lambda: (worker, SERVICE) not in self.workers.links,
                              "helper socket must retire")

    async def stop_authority(self, worker):
        task = self.authority_tasks.pop(worker, None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        daemon = self.daemons.get(worker)
        if daemon and id(daemon) not in self.closed_apps:
            for store in daemon.app.stores.values():
                store.close()
            self.closed_apps.add(id(daemon))

    async def test_controller_blocks_repair_and_root_publication_use_real_tls_helpers(self):
        from biomerte.checkpoint_chunks import STATE_FORMAT, BLOCK_BYTES, canonical
        import hashlib
        primary, standby = self.helpers["pi"], self.helpers["mac"]
        key = "checkpoint-artifact"
        raw = canonical([["artifacts", key, "payload" * (BLOCK_BYTES // 3)]])
        desc = primary.store.chunks.write_shard(
            "artifacts", hashlib.sha256(key.encode()).hexdigest()[:2], raw)
        state = dict(format=STATE_FORMAT, authority=self.identity["node"],
                     site="integration-site", shards=[desc])
        packet = primary.store.advance(state, self.binding)
        result = await primary.publish_packet(self.binding, packet)
        self.assertEqual("complete", primary.replication)
        self.assertEqual(checkpoint_ref(packet), result["checkpoint"])
        self.assertEqual(packet, standby.store.get(checkpoint_ref(packet)))
        standby.store.require_chunks(state)
        self.assertEqual({}, self.workers.pending)
        # A same-root retry uses the exact retained block set, but a corrupted
        # recipient must refuse its root ACK; the sender clears its optimization.
        primary.config["authority_kind"] = "controller"
        missing = desc["blocks"][0]["digest"]
        path = pathlib.Path(standby.store.chunks.directory) / missing
        path.write_bytes(b"z" * path.stat().st_size)
        await primary.publish_packet(self.binding, packet)
        standby.store.require_chunks(state)
        self.assertEqual(primary.store.chunks.get(missing), standby.store.chunks.get(missing))
        self.assertNotIn("package", self.workers.head(SERVICE))

    async def cleanup(self):
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        if hasattr(self, "client"):
            await self.client.close()
        for service in reversed(self.gateway_objects):
            await service.close()
        for runner in reversed(self.runners):
            await runner.cleanup()
        for daemon in self.daemons.values():
            if id(daemon) not in self.closed_apps:
                for store in daemon.app.stores.values():
                    store.close()
        for helper in self.helpers.values():
            helper.store.close()

    async def eventually(self, predicate, message):
        deadline = asyncio.get_running_loop().time() + 5
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                self.fail(message)
            await asyncio.sleep(.02)

    async def post(self, url, body, *, token=None):
        headers = {"Authorization": "Bearer " + token} if token else {}
        async with self.client.post(url, json=body, headers=headers) as response:
            text = await response.text()
            try:
                value = json.loads(text)
            except ValueError:
                value = {"error": text}
            return response.status, value

    def adapter_request(self, operation, current_binding, **fields):
        return dict(protocol=ADAPTER_PROTOCOL, operation=operation,
            id=operation + "-" + current_binding["grant_token"], binding=current_binding, **fields)

    async def prepare(self, worker, grant):
        current_binding = self.coordinator_module.grant_binding(grant)
        request = self.adapter_request("prepare", current_binding)
        status, reply = await self.post(self.configs[worker]["adapter_url"], request, token="h" * 48)
        self.assertEqual(200, status, reply)
        self.assertEqual(current_binding, reply["binding"])
        self.assertEqual("prepared", reply["phase"])
        proof = dict(protocol=ADAPTER_PROTOCOL, binding=current_binding, **reply["state"])
        assignment = self.heartbeat(worker, grant, proof=proof)["assignments"][0]
        self.assertTrue(assignment["state_ready"])
        self.assertFalse(assignment["effect_ready"])
        return request, reply, proof

    async def start_authority(self, worker, grant, prepared):
        current_binding = self.coordinator_module.grant_binding(grant)
        names = ("ID", "REVISION", "WORKER", "INCARNATION", "BOOT_EPOCH", "GRANT")
        env = {"BIOME_SERVICE_" + name: current_binding[key]
               for name, key in zip(names, self.coordinator_module.BINDING_KEYS)}
        env["BIOME_SERVICE_CHECKPOINT"] = prepared["state"]["checkpoint"]
        env["BIOME_SERVICE_ADAPTER_URL"] = self.configs[worker]["adapter_url"]
        with patch.dict(os.environ, env):
            daemon = AuthorityDaemon(self.configs[worker], self.client)
            self.daemons[worker] = daemon
            app = web.Application()
            app.router.add_post("/fence", daemon.fence)
            app.router.add_get("/health", daemon.health)
            base, unused = await self.server(app)
            self.configs[worker]["child_url_template"] = base
            task = asyncio.create_task(daemon.authority_loop())
            self.tasks.append(task)
            self.authority_tasks[worker] = task
            await self.eventually(lambda: daemon.connected, "prepared child must register exact authority")
        return daemon

    def signed(self, operation, arguments, *, consumer=False, request_id=None):
        body = dict(site="integration-site", realm="site", operation=operation,
                    arguments=arguments, id=request_id or uuid.uuid4().hex)
        if consumer:
            body["controller"] = self.identity["node"]
        actor, key = ((self.consumer_actor, self.consumer_key) if consumer
                      else (self.provider_actor, self.provider_key))
        return dict(body, auth=dict(actor=actor, signature=sign(key, body)))

    async def call(self, envelope):
        return await self.post(self.origin + "/api/service-authorities/call", envelope)

    async def activate(self, worker, grant, *, check_gates=False):
        current_binding = self.coordinator_module.grant_binding(grant)
        request, prepared, proof = await self.prepare(worker, grant)
        daemon = await self.start_authority(worker, grant, prepared)
        if check_gates:
            status, value = await self.call(self.signed("cap.acquire",
                dict(contract=CONTRACT, resource=RESOURCE, task="blocked"), consumer=True))
            self.assertEqual(503, status, value)
        status, value = await self.call(self.signed("provider.poll", {"provider": self.provider_actor}))
        self.assertEqual(200, status, value)
        poll = value["result"]
        self.assertEqual([], poll["actions"])
        self.assertEqual([], poll["streams"])
        self.assertEqual(1, len(poll["fences"]))
        lease = poll["fences"][0]
        status, reply = await self.call(self.signed("cap.fenced", dict(
            provider=self.provider_actor, lease=lease["id"], generation=lease["generation"])))
        self.assertEqual(200, status, reply)
        fence_url = self.configs[worker]["adapter_url"].rsplit("/", 1)[0] + "/fence"
        status, fence = await self.post(fence_url, dict(current_binding, prepared_state=proof),
                                        token="h" * 48)
        self.assertEqual(200, status, fence)
        self.assertEqual(prepared["state"]["checkpoint"], fence["checkpoint"])
        self.assertEqual(current_binding, fence["binding"])
        assignment = self.heartbeat(worker, grant, proof=proof, fence=fence)["assignments"][0]
        self.assertTrue(assignment["effect_ready"])
        self.assertTrue(self.coordinator.validate(current_binding)["valid"])
        if check_gates:
            # Coordinator fencing readiness cannot bypass the child's own
            # durable active-phase check.
            status, value = await self.call(self.signed("cap.acquire",
                dict(contract=CONTRACT, resource=RESOURCE, task="still-blocked"), consumer=True))
            self.assertEqual(503, status, value)
        activation = self.adapter_request("activate", current_binding,
            checkpoint=prepared["state"]["checkpoint"], fence_ack=fence)
        status, active = await self.post(self.configs[worker]["adapter_url"], activation,
                                         token="h" * 48)
        self.assertEqual(200, status, active)
        self.assertEqual("active", active["phase"])
        self.heartbeat(worker, grant, proof=proof, fence=fence, healthy=True)
        return daemon, prepared, proof, fence, activation, active

    async def test_real_tls_wire_prepare_signed_fence_and_activate(self):
        daemon, prepared, proof, fence, activation, active = await self.activate(
            "pi", self.grant, check_gates=True)
        self.assertEqual(185, fence["resources"][RESOURCE]["generation"])
        head = self.workers.head(SERVICE)
        self.assertNotEqual(prepared["state"]["checkpoint"], head["digest"])
        self.assertTrue(self.coordinator.validate(self.binding)["valid"])
        self.assertEqual(checkpoint_ref(self.helpers["pi"].store.get(head)),
                         checkpoint_ref(self.helpers["mac"].store.get(head)))
        malformed = self.signed("provider.poll", {"provider": self.provider_actor})
        malformed["arguments"]["limit"] = 1
        status, value = await self.call(malformed)
        self.assertIn(status, (400, 403), value)
        self.assertNotIn("result", value)
        status, value = await self.call(self.signed("provider.poll", {"provider": self.provider_actor}))
        self.assertEqual(200, status, value)
        self.assertEqual([], value["result"]["fences"])
        child = self.configs["pi"]["child_url_template"]
        async with self.client.get(child + "/health") as response:
            self.assertEqual(200, response.status)
            self.assertTrue((await response.json())["ready"])

    async def test_prepare_and_activation_lost_reply_are_exactly_idempotent(self):
        request, prepared, proof = await self.prepare("pi", self.grant)
        status, repeated = await self.post(self.configs["pi"]["adapter_url"], request, token="h" * 48)
        self.assertEqual(200, status, repeated)
        self.assertEqual(prepared, repeated)
        daemon, prepared2, proof, fence, request, active = await self.activate("pi", self.grant)
        status, repeated = await self.post(self.configs["pi"]["adapter_url"], request, token="h" * 48)
        self.assertEqual(200, status, repeated)
        self.assertEqual(active, repeated)
        self.assertEqual(185, fence["resources"][RESOURCE]["generation"])
        envelope = self.signed("cap.acquire", dict(contract=CONTRACT, resource=RESOURCE,
            task="stable-acquire", enqueue=False), consumer=True, request_id="lost-acquire-reply")
        status, first = await self.call(envelope)
        self.assertEqual(200, status, first)
        status, second = await self.call(envelope)
        self.assertEqual(200, status, second)
        self.assertEqual(first["result"]["id"], second["result"]["id"])

    async def test_current_witness_allows_survivor_takeover_and_old_binding_cannot_answer(self):
        old, prepared, proof, fence, unused, active = await self.activate("pi", self.grant)
        witnessed = dict(self.workers.head(SERVICE))
        await self.stop_authority("pi")
        await self.stop_helper("pi")
        self.clock.advance(6)
        new_grant = self.heartbeat("mac")["assignments"][0]
        self.assertNotEqual(self.grant["grant_token"], new_grant["grant_token"])
        new, prepared2, proof2, fence2, unused, active2 = await self.activate("mac", new_grant)
        self.assertEqual(witnessed["digest"], prepared2["state"]["checkpoint"])
        self.assertGreater(fence2["resources"][RESOURCE]["generation"],
                           fence["resources"][RESOURCE]["generation"])
        self.assertEqual("degraded", self.helpers["mac"].replication)
        old_reply = await old.dispatch(dict(type="call", id="old-resumed",
            binding=self.binding, envelope=self.signed("provider.poll", {"provider": self.provider_actor})))
        self.assertEqual(503, old_reply["status"])
        self.assertNotIn("result", old_reply)
        with self.assertRaises(Fault):
            await old.maintain_once()
        self.assertTrue(self.coordinator.validate(
            self.coordinator_module.grant_binding(new_grant))["valid"])

    async def test_takeover_fetches_current_packet_from_available_previous_host(self):
        daemon, prepared, proof, fence, unused, active = await self.activate("pi", self.grant)
        await self.stop_helper("mac")
        daemon.app.stores["site"].execute(lambda tx: tx.put("integration", "latest", {"value": 7}))
        await daemon.publish()
        head = dict(self.workers.head(SERVICE))
        with self.assertRaises(ValueError):
            self.helpers["mac"].store.get(head)
        self.start_helper("mac")
        await self.eventually(lambda: ("mac", SERVICE) in self.workers.links, "standby helper reconnects")
        await self.stop_authority("pi")
        self.coordinator.release(self.binding)
        self.clock.advance(6)
        grant = self.heartbeat("mac")["assignments"][0]
        request, prepared, proof = await self.prepare("mac", grant)
        self.assertEqual(head["digest"], prepared["state"]["checkpoint"])
        packet = self.helpers["mac"].store.get(head)
        self.assertIn(["integration", "latest", {"value": 7}], packet["payload"]["state"]["records"])
        self.assertEqual("complete", self.helpers["mac"].replication)

    async def test_degraded_writer_high_water_blocks_stale_survivor(self):
        daemon, prepared, proof, fence, unused, active = await self.activate("pi", self.grant)
        await self.stop_helper("mac")
        daemon.app.stores["site"].execute(lambda tx: tx.put("integration", "solo-write", {"value": 1}))
        await daemon.publish()
        current = dict(self.workers.head(SERVICE))
        self.assertEqual("degraded", self.helpers["pi"].replication)
        with self.assertRaises(ValueError):
            self.helpers["mac"].store.get(current)
        await self.stop_authority("pi")
        await self.stop_helper("pi")
        self.clock.advance(6)
        new_grant = self.heartbeat("mac")["assignments"][0]
        self.start_helper("mac")
        await self.eventually(lambda: ("mac", SERVICE) in self.workers.links, "survivor helper reconnects")
        request = self.adapter_request("prepare", self.coordinator_module.grant_binding(new_grant))
        status, reply = await self.post(self.configs["mac"]["adapter_url"], request, token="h" * 48)
        self.assertIn(status, (409, 503), reply)
        self.assertFalse(self.coordinator.validate(
            self.coordinator_module.grant_binding(new_grant))["valid"])

    async def test_real_witness_restart_without_peer_never_blesses_cached_state(self):
        await self.activate("pi", self.grant)
        await self.stop_authority("pi")
        await self.stop_helper("mac")
        await self.services.close()
        await self.workers.close()
        await self.gateway_runner.cleanup()
        await self.start_gateway()
        for worker, helper in self.helpers.items():
            self.configs[worker]["coordinator_origin"] = self.origin
            helper.transport.origin = self.origin
        self.heartbeat("pi")
        self.clock.advance(5)
        new_grant = self.heartbeat("pi")["assignments"][0]
        self.assertIsNone(self.workers.head(SERVICE))
        await self.eventually(lambda: ("pi", SERVICE) in self.workers.links, "helper reconnects to new witness")
        request = self.adapter_request("prepare", self.coordinator_module.grant_binding(new_grant))
        status, reply = await self.post(self.configs["pi"]["adapter_url"], request, token="h" * 48)
        self.assertIn(status, (409, 503), reply)
        self.assertIsNone(self.workers.head(SERVICE))
        self.assertFalse(new_grant["state_ready"])

    async def test_helper_follows_supervisor_incarnation_without_waiting_for_rpc(self):
        self.clock.advance(11)
        self.heartbeat("mac", incarnation="mac-2")
        await self.eventually(
            lambda: self.workers.links.get(("mac", SERVICE), {}).get("key") == ("mac", "mac-2"),
            "helper must refresh the supervisor incarnation while its WS remains connected")


    async def acquire_stream(self, task="stream"):
        status, value = await self.call(self.signed("cap.acquire",
            dict(task=task, contract=CONTRACT, resource=RESOURCE), consumer=True))
        self.assertEqual(200, status, value)
        lease = value["result"]
        self.assertEqual("fencing", lease["state"])
        status, poll = await self.call(self.signed("provider.poll", {"provider": self.provider_actor}))
        self.assertEqual(200, status, poll)
        self.assertIn(lease["id"], [row["id"] for row in poll["result"]["fences"]])
        status, fenced = await self.call(self.signed("cap.fenced", dict(
            provider=self.provider_actor, lease=lease["id"], generation=lease["generation"])))
        self.assertEqual(200, status, fenced)
        status, opened = await self.call(self.signed("stream.open",
            dict(lease=lease["id"], ttl_seconds=60), consumer=True))
        self.assertEqual(200, status, opened)
        return lease, opened["result"]

    async def test_signed_stream_expiry_is_rejected_and_scoped_maintenance_allows_reopen(self):
        daemon, unused, proof, fence, unused, active = await self.activate("pi", self.grant)
        lease, stream = await self.acquire_stream()
        for consumer, role in ((True, "server"), (False, "provider")):
            status, attached = await self.call(self.signed("stream.attach",
                dict(session=stream["id"], role=role), consumer=consumer))
            self.assertEqual(200, status, attached)
            self.assertTrue(attached["result"]["attachment_credential"])
        before = self.workers.head(SERVICE)["sequence"]
        with patch("time.time", return_value=stream["expires_at"] + 1):
            # Expiry is checked directly: an expired attachment is rejected even
            # before the scoped maintenance transaction deletes the old grant.
            for name, arguments in (
                    ("stream.inspect", {"session": stream["id"]}),
                    ("stream.attach", {"session": stream["id"], "role": "server"})):
                status, value = await self.call(self.signed(name, arguments, consumer=True))
                self.assertEqual(409, status, value)
            status, poll = await self.call(self.signed("provider.poll", {"provider": self.provider_actor}))
            self.assertEqual(200, status, poll)
            self.assertNotIn(stream["id"], [row["id"] for row in poll["result"]["streams"]])
            await daemon.maintain_once()
            status, held = await self.call(self.signed("cap.lease", {"lease": lease["id"]}, consumer=True))
            self.assertEqual(200, status, held)
            # Persistent leases intentionally have no heartbeat/watch timeout.
            self.assertEqual("bound", held["result"]["state"])
            status, reopened = await self.call(self.signed("stream.open",
                dict(lease=lease["id"], ttl_seconds=60), consumer=True))
            self.assertEqual(200, status, reopened)
            self.assertNotEqual(stream["id"], reopened["result"]["id"])
            status, closed = await self.call(self.signed("stream.close",
                dict(session=reopened["result"]["id"]), consumer=True))
            self.assertEqual(200, status, closed)
            status, released = await self.call(self.signed("cap.lease",
                {"lease": lease["id"]}, consumer=True))
            self.assertEqual(200, status, released)
            self.assertEqual("released", released["result"]["state"])
        head = self.workers.head(SERVICE)
        self.assertGreater(head["sequence"], before)
        self.assertEqual(checkpoint_ref(self.helpers["pi"].store.get(head)),
                         checkpoint_ref(self.helpers["mac"].store.get(head)))

    async def test_scoped_maintenance_recovers_fence_timeout_and_promotes_waiter(self):
        daemon, unused, proof, fence, unused, active = await self.activate("pi", self.grant)
        # A streaming offer intentionally shares its persistent reservation.
        # This timeout case uses an exclusive nonstreaming interface so the
        # second independent request actually waits for the first fence.
        status, offered = await self.call(self.signed("cap.offer", dict(
            provider=self.provider_actor, contract=CONTRACT, resource=RESOURCE,
            configuration={}, capacity=1, lease_class="persistent")))
        self.assertEqual(200, status, offered)
        leases = []
        for task, expected in (("missing-provider-fence", "fencing"), ("queued-next", "queued")):
            status, value = await self.call(self.signed("cap.acquire",
                dict(task=task, contract=CONTRACT, resource=RESOURCE), consumer=True))
            self.assertEqual(200, status, value)
            self.assertEqual(expected, value["result"]["state"])
            leases.append(value["result"])
        before = self.workers.head(SERVICE)["sequence"]
        expires = leases[0]["fencing_since"] + leases[0]["lease_policy"]["fence_ttl_seconds"] + 1
        with patch("time.time", return_value=expires):
            # provider.poll is read-only and cannot settle these on its own.
            status, prior = await self.call(self.signed("provider.poll", {"provider": self.provider_actor}))
            self.assertEqual(200, status, prior)
            self.assertIn(leases[0]["id"], [row["id"] for row in prior["result"]["fences"]])
            await daemon.maintain_once()
            states = []
            for lease in leases:
                status, value = await self.call(self.signed("cap.lease",
                    {"lease": lease["id"]}, consumer=True))
                self.assertEqual(200, status, value)
                states.append(value["result"])
            self.assertEqual(["released", "fencing"], [row["state"] for row in states])
            self.assertGreater(states[1]["generation"], states[0]["generation"])
            status, poll = await self.call(self.signed("provider.poll", {"provider": self.provider_actor}))
            self.assertEqual(200, status, poll)
            self.assertEqual([leases[1]["id"]], [row["id"] for row in poll["result"]["fences"]])
        head = self.workers.head(SERVICE)
        self.assertGreater(head["sequence"], before)
        self.assertEqual(checkpoint_ref(self.helpers["pi"].store.get(head)),
                         checkpoint_ref(self.helpers["mac"].store.get(head)))


    async def test_legacy_relay_and_scoped_controller_share_workers_with_complete_enrollment(self):
        from tests.legacy_checkpoint_helper_fixture import LegacyCheckpointHelper
        from biomerte.checkpoint_chunks import STATE_FORMAT, canonical
        import hashlib

        # Run the actual deployed registration, RPC, prepare and publish methods.
        # The same durable fixture stores survive this isolated protocol switch.
        for worker in ("pi", "mac"):
            await self.stop_helper(worker)
            self.helpers[worker].__class__ = LegacyCheckpointHelper
            self.start_helper(worker)
        await self.eventually(lambda: len(self.workers.links) == 2,
                              "legacy helpers must answer the pre-ACK status probe")
        self.assertTrue(all(link["legacy"] for link in self.workers.links.values()))

        controller_service = "controller-runtime"
        aliases = ["1" * 64, "2" * 64]
        self.coordinator.reserved_authorities = frozenset(aliases)
        controller_private = self.root / "controller-private"
        controller_identity = initialize(str(controller_private), "integration-site")
        controller_spec = dict(self.spec, id=controller_service,
            authority=dict(kind="controller", id=controller_identity["node"],
                           resources=[], providers=[], controller_aliases=aliases),
            activation=dict(protocol=ADAPTER_PROTOCOL, binding=controller_service))
        controller_spec["revision"] = self.coordinator_module.manifest_revision(controller_spec)
        controller_grant = None
        for worker in ("pi", "mac"):
            row = dict(manifest=controller_spec, manifest_revision=controller_spec["revision"],
                platform="linux/arm64", image_ref=controller_spec["images"]["linux/arm64"],
                prepared=True, dependencies_ready=True, healthy=None)
            result = self.coordinator.heartbeat(dict(worker_id=worker,
                worker_incarnation=worker + "-1", services=[row]))
            self.assertEqual({}, result["errors"])
            if worker == "pi":
                controller_grant = result["assignments"][0]
        controller_binding = self.coordinator_module.grant_binding(controller_grant)
        checkpoint_key = self.root / "controller-checkpoint.key"
        checkpoint_key.write_text("controller-key-" + "c" * 48)
        controllers, controller_tasks = {}, {}
        for worker in ("pi", "mac"):
            config = dict(self.common, authority_kind="controller",
                service_id=controller_service, authority_id=controller_identity["node"],
                controller_aliases=aliases, operator_actors=[],
                checkpoint_key_file=str(checkpoint_key), private_dir=str(controller_private),
                worker_id=worker, checkpoint_db=str(self.root / ("controller-" + worker) / "checkpoint.sqlite"),
                authority_home=str(self.root / ("controller-" + worker) / "authority"))
            helper = CheckpointHelper(config, self.client)
            controllers[worker] = helper
            self.helpers["controller-" + worker] = helper
        primary, standby = controllers["pi"], controllers["mac"]
        artifact_id = "controller-only"
        raw = canonical([["artifacts", artifact_id, "initial controller state"]])
        prefix = hashlib.sha256(artifact_id.encode()).hexdigest()[:2]
        desc = primary.store.chunks.write_shard("artifacts", prefix, raw)
        controller_state = dict(format=STATE_FORMAT, authority=controller_identity["node"],
                                site="integration-site", shards=[desc])
        packet = primary.store.bootstrap(controller_state, {"source_receipt": "isolated-controller"})
        for block in desc["blocks"]:
            standby.store.chunks.put(primary.store.chunks.get(block["digest"]), block["digest"])
        standby.store.install(packet)

        def start_controller(worker):
            task = asyncio.create_task(controllers[worker].worker_loop())
            self.tasks.append(task)
            controller_tasks[worker] = task

        for worker in ("pi", "mac"):
            start_controller(worker)
        expected = {(worker, service) for worker in ("pi", "mac")
                    for service in (SERVICE, controller_service)}
        await self.eventually(lambda: set(self.workers.links) == expected,
                              "two services must coexist on each physical worker")
        for worker in ("pi", "mac"):
            self.assertTrue(self.workers.links[(worker, SERVICE)]["legacy"])
            self.assertFalse(self.workers.links[(worker, controller_service)]["legacy"])

        async def status_for(helper):
            result = await helper.transport.request("/api/service-checkpoints/status",
                {"service_id": helper.config["service_id"], "workers": []})
            self.assertEqual({"pi", "mac"}, {row["worker_id"] for row in result["workers"]})
            self.assertEqual(2, len(result["workers"]))
            self.assertTrue(all(row["status"] == 200 and row["result"]["durable"]
                                for row in result["workers"]))
            self.assertTrue(all(row["checkpoint"]["service_id"] == helper.config["service_id"]
                                for row in result["workers"]))
            return result

        await asyncio.gather(status_for(self.helpers["pi"]), status_for(primary))
        for worker in ("pi", "mac"):
            requests = []
            for helper in (self.helpers[worker], controllers[worker]):
                requests.append(helper.transport.request("/api/service-checkpoints/get", {
                    "service_id": helper.config["service_id"],
                    "worker": {"worker_id": worker, "worker_incarnation": worker + "-1"},
                    "checkpoint": helper.store.head()}))
            relay_reply, controller_reply = await asyncio.gather(*requests)
            self.assertEqual(self.genesis, relay_reply["result"]["package"])
            self.assertEqual(packet, controller_reply["result"]["package"])

        # No first witness can be created from one replica of either service.
        # The other service's same-host socket cannot stand in for a missing copy.
        await self.stop_helper("mac")
        with self.assertRaises((Fault, CheckpointError)):
            await self.helpers["pi"].prepare(self.adapter_request("prepare", self.binding))
        self.assertIsNone(self.workers.head(SERVICE))
        await status_for(primary)
        self.start_helper("mac")
        await self.eventually(lambda: ("mac", SERVICE) in self.workers.links, "relay must reconnect")
        await self.prepare("pi", self.grant)
        self.assertEqual("complete", self.helpers["pi"].replication)
        relay_head = dict(self.workers.head(SERVICE))

        controller_tasks["mac"].cancel()
        await asyncio.gather(controller_tasks["mac"], return_exceptions=True)
        await self.eventually(lambda: ("mac", controller_service) not in self.workers.links,
                              "only the controller helper must retire")
        request = self.adapter_request("prepare", controller_binding)
        with self.assertRaises((Fault, CheckpointError)):
            await primary.prepare(request)
        self.assertIsNone(self.workers.head(controller_service))
        await status_for(self.helpers["pi"])
        start_controller("mac")
        await self.eventually(lambda: ("mac", controller_service) in self.workers.links,
                              "controller must reconnect without disturbing relay")
        prepared = await primary.prepare(request)
        self.assertEqual("prepared", prepared["phase"])
        self.assertEqual("complete", primary.replication)

        next_raw = canonical([["artifacts", artifact_id, "new controller state"]])
        next_desc = primary.store.chunks.write_shard("artifacts", prefix, next_raw)
        advanced = primary.store.advance(dict(controller_state, shards=[next_desc]), controller_binding)
        published = await primary.publish_packet(controller_binding, advanced, require_all=True)
        self.assertEqual(checkpoint_ref(advanced), published["checkpoint"])
        self.assertEqual(advanced, standby.store.get(checkpoint_ref(advanced)))
        standby.store.require_chunks(advanced["payload"]["state"])
        self.assertEqual(relay_head, self.workers.head(SERVICE))
        self.assertEqual(expected, set(self.workers.links))
        self.assertEqual({}, self.workers.pending)

        # A live supervisor and a sibling relay link do not provide a
        # controller-checkpoint transport. Recover from the witnessed head
        # while the missing controller helper is disconnected.
        controller_tasks["mac"].cancel()
        await asyncio.gather(controller_tasks["mac"], return_exceptions=True)
        await self.eventually(lambda: ("mac", controller_service) not in self.workers.links,
                              "standby controller helper disconnects")
        self.assertIn(("mac", SERVICE), self.workers.links)
        self.assertIn("mac", (await primary.transport.candidates())[1])
        same_state = primary.store.advance(dict(controller_state, shards=[next_desc]), controller_binding)
        published = await primary.publish_packet(controller_binding, same_state)
        self.assertEqual(checkpoint_ref(same_state), published["checkpoint"])
        self.assertEqual([], published["acknowledgements"])
        self.assertEqual("degraded", primary.replication)
        self.assertEqual(relay_head, self.workers.head(SERVICE))

        body = dict(binding=controller_binding, state=same_state["payload"]["state"])
        with patch.object(primary, "publish_packet", wraps=primary.publish_packet) as republish:
            cached = await primary.publish(body)
            self.assertEqual(checkpoint_ref(same_state), cached["checkpoint"])
            republish.assert_not_awaited()
            # An unchanged root must still catch up a newly registered helper.
            start_controller("mac")
            await self.eventually(lambda: ("mac", controller_service) in self.workers.links,
                                  "standby controller helper rejoins")
            primary.controller_checkpoint_refresh_at = 0
            refreshed = await primary.publish(body)
            republish.assert_awaited_once()
        self.assertEqual(checkpoint_ref(same_state), refreshed["checkpoint"])
        self.assertEqual("complete", refreshed["replication"])
        self.assertEqual(same_state, standby.store.get(checkpoint_ref(same_state)))

    async def test_private_helper_https_requires_trusted_certificate_and_completes_activation(self):
        helper = self.helpers["pi"]
        app = web.Application()
        app.router.add_post("/adapter", helper.handle)
        app.router.add_post("/fence", helper.handle)
        base, unused = await self.server(app, tls=True)
        self.configs["pi"].update(adapter_url=base + "/adapter", adapter_ca_file=str(self.cert))
        request = dict(operation="status", binding=self.binding)
        async with ClientSession() as verified:
            with self.assertRaises(ClientConnectorCertificateError):
                await local_rpc(verified, base + "/adapter", "h" * 48, request, timeout=2)
            await self.prepare("pi", self.grant)
            phase = await local_rpc(verified, base + "/adapter", "h" * 48, request,
                                    timeout=2, ca_file=str(self.cert))
            self.assertEqual(self.binding, phase["binding"])
            self.assertEqual("prepared", phase["phase"])
        daemon, unused, proof, fence, unused, active = await self.activate("pi", self.grant)
        self.assertEqual("active", (await daemon.phase())["phase"])


if __name__ == "__main__":
    unittest.main()
