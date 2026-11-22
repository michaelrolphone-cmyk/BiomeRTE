"""Exercise controller leases, gateway placement and Docker cleanup on one Engine."""
import importlib.util
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

from biomerte import docker_services as ds
from tests import test_controller_authority


class GatewayClient:
    def __init__(self, coordinator, authority, manifest):
        self.coordinator = coordinator
        self.authority = authority
        self.manifest = manifest

    def call(self, operation, payload):
        scope = dict(self.manifest["capability_lease"])
        del scope["controller"]
        lease = self.authority.capability_demand(scope)["lease"]
        binding = (lease["id"], lease["generation"]) if lease else None
        self.coordinator.record_capability_demand(
            self.manifest["id"], self.manifest["revision"], binding, time.monotonic() + 3)
        return self.coordinator.heartbeat(payload)


class RealLease(test_controller_authority.ControllerTests):
    def test_disposable_worker(self):
        gateway = Path(os.environ["BIOME_TEST_COORDINATOR"])
        spec = importlib.util.spec_from_file_location("lease_test_coordinator", gateway)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        worker = "lease-test-" + uuid.uuid4().hex
        engine = ds.DockerEngine(os.environ.get("BIOME_TEST_DOCKER_SOCKET", "/var/run/docker.sock"))
        version = engine.version()
        platform = ds.normalize_platform(version["Os"], version["Arch"])
        image = engine.image(os.environ["BIOME_TEST_LEASE_IMAGE"])["Id"]
        scope = dict(provider=self.peer, resource=worker, contract="docker.fixture:1")
        manifest = dict(id=worker, images={platform: image}, command=["sleep", "600"],
                        health={"command": ["sh", "-c", "test -n \"$BIOME_CAPABILITY_LEASE_ID\""],
                                "interval_s": 1}, effect_policy="stateless", state_policy="lease_scoped",
                        capability_lease=dict(scope, controller=self.node["node"]),
                        lease_ttl_s=3)
        manifest["revision"] = ds.revision(manifest)
        manifest_file = Path(self.tmp.name) / "manifest.json"
        manifest_file.write_text(json.dumps(manifest))
        coordinator = module.Coordinator("test-only", max_lease_ttl_s=3)
        coordinator.configure(manifest)
        config = dict(worker_id=worker, state_file=os.path.join(self.tmp.name, "runner.sqlite"),
                      manifest_files=[str(manifest_file)])
        client = GatewayClient(coordinator, self.authority, manifest)
        runner = ds.Runner(config, engine=engine, client=client)

        def cleanup():
            runner.close()
            for row in engine.containers(worker):
                engine.remove_managed(row["Id"], worker)
        self.addCleanup(cleanup)

        def until(condition):
            deadline = time.monotonic() + 45
            while True:
                runner.tick()
                if condition():
                    return
                self.assertLess(time.monotonic(), deadline,
                                str(coordinator.status()["services"]))
                time.sleep(.2)

        self.call("cap.define", dict(name="docker.fixture", inputs={}, outputs={"system": "str"}), "define-lease")
        self.call("peer.trust", dict(public_key=self.peer_public, label="docker-fixture-worker",
                                     scopes=["cap.offer", "cap.fenced", "provider.claim", "provider.result"]),
                  "enroll-worker")
        self.call("cap.offer", dict(scope, capacity=1), "offer-lease", peer=True)
        runner.tick()
        self.assertEqual([], engine.containers(worker))

        def acquire(request):
            return self.call("cap.acquire", dict(resource=worker, contract=scope["contract"],
                                                 task=request), request)

        def healthy():
            return coordinator.status()["services"][worker]["state"] == "active"

        def mounts(child):
            return [m["Name"] for m in engine.inspect(child)["Mounts"] if m["Type"] == "volume"]

        def assert_removed(child, volumes):
            self.assertIsNone(engine.inspect(child))
            names = subprocess.check_output(["docker", "volume", "ls", "-q"], text=True).splitlines()
            self.assertTrue(set(volumes).isdisjoint(names))

        # A foreign container and named volume must survive release and recovery.
        volume = subprocess.check_output(["docker", "volume", "create", worker], text=True).strip()
        self.addCleanup(subprocess.run, ["docker", "volume", "rm", volume], check=False)
        foreign = subprocess.check_output(["docker", "create", "-v", volume + ":/lease-tmp",
                                           image, "true"], text=True).strip()
        self.addCleanup(subprocess.run, ["docker", "rm", "-v", foreign], check=False)

        lease = acquire("lease-one")
        until(healthy)
        child = runner.containers[worker]
        volumes = mounts(child)
        self.assertTrue(volumes, "fixture image must declare an anonymous volume")
        self.assertEqual(lease["id"], engine.inspect(child)["Config"]["Labels"][ds.LEASE])
        self.assertEqual(lease["id"], acquire("lease-one")["id"])
        runner.tick()
        self.assertEqual([child], [row["Id"] for row in engine.containers(worker)])
        self.call("cap.fenced", dict(provider=scope["provider"], lease=lease["id"],
                                     generation=lease["generation"]), "fence-one", peer=True)
        self.call("cap.invoke", dict(lease=lease["id"], operation="container-system", inputs={},
                                     release_after_result=False), "submit-container-system")
        custody = dict(provider=scope["provider"], operation="container-system", generation=lease["generation"])
        self.call("provider.claim", custody, "claim-container-system", peer=True)
        system = subprocess.check_output(["docker", "exec", child, "uname", "-s"], text=True).strip()
        self.call("provider.result", dict(custody, state="succeeded", result={"system": system}),
                  "return-container-system", peer=True)
        receipt = self.call("cap.status", dict(operation="container-system"), "get-container-system")
        self.assertEqual({"system": "Linux"}, receipt["result"])
        self.call("cap.release", dict(lease=lease["id"]), "release-one")
        until(lambda: not engine.containers(worker) and not runner.pending_cleanup)
        assert_removed(child, volumes)

        lease = acquire("lease-two")
        until(healthy)
        child = runner.containers[worker]
        volumes = mounts(child)
        # Lose the supervisor without running close(). Its durable child record
        # and Docker labels must allow a fresh incarnation to reclaim custody.
        runner = ds.Runner(config, engine=engine, client=client)
        until(lambda: worker in runner.containers and runner.containers[worker] != child and healthy())
        assert_removed(child, volumes)
        current = runner.containers[worker]
        current_volumes = mounts(current)
        self.assertEqual(str(lease["generation"]), engine.inspect(current)["Config"]["Labels"][ds.GENERATION])
        # Stop delivering coordinator heartbeats. Drive only the real expiry watchdog path.
        deadline = time.monotonic() + 15
        while engine.containers(worker) or runner.pending_cleanup:
            runner.expire()
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.1)
        assert_removed(current, current_volumes)
        self.call("cap.release", dict(lease=lease["id"]), "release-two")
        self.assertIsNotNone(engine.inspect(foreign))
        subprocess.run(["docker", "volume", "inspect", volume], check=True, stdout=subprocess.DEVNULL)
