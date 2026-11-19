#!/usr/bin/env python3
"""Qualify the real Docker runner against a real HTTP coordinator.

Uses two isolated worker containers on one Docker host. This is a two-worker
mechanism test, not proof of Pi/Mac hardware failover or physical relay fencing.
Never stops unrelated containers. Requires a built runner image and the
coordinator source checkout plus aiohttp in the invoking Python environment.
"""
import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CHILD = """
import os,json,urllib.request,urllib.error
from http.server import HTTPServer,BaseHTTPRequestHandler
binding={k:os.environ[e] for k,e in {
 'service_id':'BIOME_SERVICE_ID','manifest_revision':'BIOME_SERVICE_REVISION',
 'worker_id':'BIOME_SERVICE_WORKER','worker_incarnation':'BIOME_SERVICE_INCARNATION',
 'boot_epoch':'BIOME_SERVICE_BOOT_EPOCH','grant_token':'BIOME_SERVICE_GRANT'}.items()}
class H(BaseHTTPRequestHandler):
 def do_GET(self):
  valid=False
  try:
   req=urllib.request.Request(os.environ['QA_GATE'],json.dumps(binding).encode(),{'Content-Type':'application/json'})
   with urllib.request.urlopen(req,timeout=2) as r:valid=json.load(r)['valid']
  except Exception:pass
  self.send_response(200 if valid else 409);self.end_headers()
  self.wfile.write(json.dumps({'valid':valid,'worker_id':binding['worker_id'],'boot_epoch':binding['boot_epoch']}).encode())
 def log_message(self,*a):pass
HTTPServer(('0.0.0.0',8080),H).serve_forever()
"""


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Fixture:
    def __init__(self, module, secret, port):
        self.module, self.secret, self.port = module, secret, port
        self.thread = None
        self.loop = None
        self.runner = None
        self.ready = threading.Event()

    def start(self):
        self.ready.clear()
        self.thread = threading.Thread(target=self._thread, daemon=True)
        self.thread.start()
        if not self.ready.wait(10):
            raise RuntimeError("Test coordinator did not start")

    def _thread(self):
        from aiohttp import web
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        async def setup():
            self.coordinator = self.module.Coordinator(self.secret, max_lease_ttl_s=8)
            app = web.Application()
            self.coordinator.mount(app)
            async def gate(request):
                result = self.coordinator.validate(await request.json())
                return web.json_response(result)
            app.router.add_post("/qa/accept", gate)
            self.runner = web.AppRunner(app)
            await self.runner.setup()
            await web.TCPSite(self.runner, "0.0.0.0", self.port).start()
            self.ready.set()
        self.loop.run_until_complete(setup())
        self.loop.run_forever()
        self.loop.run_until_complete(self.runner.cleanup())
        self.loop.close()

    def stop(self):
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(10)
        self.loop = None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coordinator-source", required=True)
    parser.add_argument("--image", default="biomerte-service-runner:local")
    parser.add_argument("--docker", default="/usr/local/bin/docker")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("qualification_coordinator", args.coordinator_source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    import sys
    sys.path.insert(0, root)
    from biomerte.docker_services import revision, normalize_platform
    run_id = uuid.uuid4().hex[:10]
    temp = tempfile.mkdtemp(prefix="biomerte-service-qa-")
    # Docker Desktop shares /Users. /var/folders temp paths also normally map,
    # but keeping test bind mounts beside the requested output is more portable.
    shutil.rmtree(temp)
    temp = os.path.join(os.path.dirname(os.path.abspath(args.output)), "qa-" + run_id)
    os.makedirs(temp)
    secret = uuid.uuid4().hex + uuid.uuid4().hex
    port = free_port()
    fixture = Fixture(module, secret, port)
    workers = ["qa-a-" + run_id, "qa-b-" + run_id]
    names = ["biomerte-service-qa-a-" + run_id, "biomerte-service-qa-b-" + run_id]
    ports = [free_port(), free_port()]
    report = {"qualification": "two Docker workers on one physical host",
              "pi_hardware_qualified": False, "relay_hardware_qualified": False,
              "checks": [], "result": "running",
              "coordinator_source_sha256": hashlib.sha256(open(args.coordinator_source, "rb").read()).hexdigest(),
              "runner_source_sha256": hashlib.sha256(open(os.path.join(root, "biomerte", "docker_services.py"), "rb").read()).hexdigest()}

    def docker(*argv, check=True):
        p = subprocess.run([args.docker] + list(argv), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if check and p.returncode:
            raise RuntimeError("Docker " + argv[0] + ": " + p.stderr[:1000])
        return p.stdout.strip()

    def record(name, **details):
        value = {"check": name, "passed": True}
        value.update(details)
        report["checks"].append(value)
        print(json.dumps(value), flush=True)

    def sample(index):
        try:
            with urlopen("http://127.0.0.1:%s/" % ports[index], timeout=2) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.loads(error.read().decode())
        except (OSError, URLError, HTTPException):
            return None, {}

    def wait_for(predicate, label, timeout=35):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(0.25)
        raise AssertionError("Timed out qualifying " + label)

    def child_ids(index):
        raw = docker("ps", "-aq", "--filter", "label=org.biomerte.service.managed=true",
                     "--filter", "label=org.biomerte.service.worker=" + workers[index])
        return raw.splitlines() if raw else []

    try:
        version = json.loads(docker("version", "--format", "{{json .Server}}"))
        image = json.loads(docker("image", "inspect", args.image))[0]
        image_id = image["Id"]
        report.update({"docker_version": version["Version"], "image_id": image_id,
                       "platform": normalize_platform(version["Os"], version["Arch"])})
        fixture.start()
        manifest = {"id": "qualified-service-" + run_id,
                    "images": {report["platform"]: image_id},
                    "effect_policy": "stateless", "state_policy": "stateless",
                    "lease_ttl_s": 6, "worker_ttl_s": 8, "startup_grace_s": 8,
                    "preferred_workers": workers,
                    "entrypoint": ["python", "-c"], "command": [CHILD],
                    "environment": {"QA_GATE": "http://host.docker.internal:%s/qa/accept" % port},
                    "health": {"command": ["python", "-c",
                         "import socket;s=socket.create_connection(('127.0.0.1',8080),1);s.close()"],
                         "interval_s": 1, "timeout_s": 1, "retries": 2}}
        manifest["revision"] = revision(manifest)
        for index in range(2):
            directory = os.path.join(temp, str(index))
            os.makedirs(os.path.join(directory, "state"))
            with open(os.path.join(directory, "key"), "w") as target:
                target.write(module.coordinator_key(secret))
            os.chmod(os.path.join(directory, "key"), 0o600)
            if index == 0:
                with open(os.path.join(directory, "seed.json"), "w") as target:
                    json.dump(manifest, target)
            config = {"worker_id": workers[index],
                      "coordinator_url": "http://host.docker.internal:" + str(port),
                      "coordinator_key_file": "/config/key", "allow_http_for_testing": True,
                      "docker_socket": "/var/run/docker.sock", "state_file": "/state/current.sqlite",
                      "manifest_files": ["/config/seed.json"] if index == 0 else [],
                      "heartbeat_interval_s": 0.5, "request_timeout_s": 2,
                      "port_overrides": {manifest["id"]: {"8080/tcp": [
                          {"HostIp": "127.0.0.1", "HostPort": str(ports[index])}]}}}
            with open(os.path.join(directory, "runner.json"), "w") as target:
                json.dump(config, target)
            docker("run", "-d", "--name", names[index],
                   "--label", "org.biomerte.service.qualification=" + run_id,
                   "--log-driver", "json-file", "--log-opt", "max-size=2m", "--log-opt", "max-file=2",
                   "-v", "/var/run/docker.sock:/var/run/docker.sock",
                   "-v", directory + ":/config:ro", "-v", directory + "/state:/state", image_id)
            if index == 0:
                wait_for(lambda: sample(0)[0] == 200, "first active Docker worker")
        record("initial active service", worker=workers[0])
        wait_for(lambda: os.path.isfile(os.path.join(temp, "1", "state", "current.sqlite")),
                 "standby durable state")
        import sqlite3
        def learned():
            try:
                with sqlite3.connect(os.path.join(temp, "1", "state", "current.sqlite")) as db:
                    return db.execute("SELECT revision FROM manifests WHERE id=?", (manifest["id"],)).fetchone() == (manifest["revision"],)
            except sqlite3.Error:
                return False
        wait_for(learned, "automatic standby manifest distribution")
        record("new service automatically reaches standby", revision=manifest["revision"])
        first_epoch = sample(0)[1]["boot_epoch"]
        docker("pause", names[0])
        start = time.monotonic()
        wait_for(lambda: sample(1)[0] == 200, "takeover after paused primary")
        elapsed = time.monotonic() - start
        assert sample(0)[0] == 409, "Old still-running service accepted a stale grant"
        assert child_ids(0), "Paused-owner child unexpectedly disappeared; stale-output case not exercised"
        record("paused primary takeover and stale output rejection", failover_seconds=round(elapsed, 3))
        docker("unpause", names[0])
        wait_for(lambda: not child_ids(0), "recovered primary cleans expired service")
        time.sleep(2)
        assert sample(1)[0] == 200 and sample(0)[0] != 200
        record("recovered worker stays standby")
        docker("kill", child_ids(1)[0])
        start = time.monotonic()
        wait_for(lambda: sample(0)[0] == 200, "failed service takeover by recovered peer")
        record("service crash triggers takeover", failover_seconds=round(time.monotonic() - start, 3))
        fixture.stop()
        fixture.start()
        assert sample(0)[0] != 200, "Previous gateway boot token survived restart"
        start = time.monotonic()
        wait_for(lambda: sample(0)[0] == 200 or sample(1)[0] == 200,
                 "gateway restart recovery", timeout=40)
        active = 0 if sample(0)[0] == 200 else 1
        assert sample(active)[1]["boot_epoch"] != first_epoch
        assert not (sample(0)[0] == 200 and sample(1)[0] == 200)
        record("gateway restart invalidates old epoch and recovers", recovery_seconds=round(time.monotonic() - start, 3))
        report["result"] = "passed"
    except Exception as error:
        report["result"] = "failed"
        report["error"] = str(error)
        report["runner_logs"] = {name: docker("logs", "--tail", "15", name, check=False) for name in names}
        print(json.dumps({"result": "failed", "error": str(error)}), flush=True)
        raise
    finally:
        for name in names:
            docker("unpause", name, check=False)
            docker("rm", "-f", name, check=False)
        for index in range(2):
            for container in child_ids(index):
                docker("rm", "-f", container, check=False)
        fixture.stop()
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w") as target:
            json.dump(report, target, sort_keys=True, indent=2)
            target.write("\n")
        shutil.rmtree(temp)
        print(json.dumps({"result": report["result"], "report": args.output}), flush=True)


if __name__ == "__main__":
    main()
