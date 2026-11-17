"""Declarative Docker services under an independent, ephemeral placement witness.

The witness grants placement, not arbitrary physical authority. Stateless jobs
must tolerate duplicate execution during a paused host. Effectful services need
a real sink fence acknowledged before their command is started. Existing Biome
capability leases and explicit ownership transfer are never changed here.
"""
from __future__ import print_function
import argparse
import hashlib
import http.client
import json
import logging
import math
import os
import platform
import re
import socket
import signal
import sqlite3
import ssl
import threading
import time
import uuid
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError

LOG = logging.getLogger("biomerte.docker_services")
PIN = re.compile(r"^(?:[^\s]+@)?sha256:[a-f0-9]{64}$")
ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}$")
MANAGED = "org.biomerte.service.managed"
WORKER = "org.biomerte.service.worker"
SERVICE = "org.biomerte.service.id"
GRANT = "org.biomerte.service.grant"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def revision(manifest):
    value = dict(manifest)
    value.pop("revision", None)
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def validate_manifest(value):
    if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not ID.match(value["id"]):
        raise ValueError("A stable service id is required")
    if value.get("revision") != revision(value):
        raise ValueError("Manifest revision must bind its complete canonical contents")
    images = value.get("images")
    if not isinstance(images, dict) or not images:
        raise ValueError("images must map Docker platforms to immutable image references")
    for key, image in images.items():
        if not isinstance(key, str) or not key.startswith("linux/") or not isinstance(image, str) or not PIN.match(image):
            raise ValueError("Each image must be a digest reference or local immutable sha256 id")
    effect = value.get("effect_policy")
    if effect not in ("stateless", "externally_fenced", "gateway_fenced"):
        raise ValueError("Explicit effect_policy is required")
    if value.get("state_policy") not in ("stateless", "externally_managed"):
        raise ValueError("Local-only authoritative state cannot automatically fail over")
    if effect != "stateless" and not isinstance(value.get("fence_hook"), dict):
        raise ValueError("Effectful services require an actual fence hook")
    if value.get("fence_hook") and not value["fence_hook"].get("url"):
        raise ValueError("Fence hook URL is required")
    if "command" in value and (not isinstance(value["command"], list) or
                              any(not isinstance(x, str) for x in value["command"])):
        raise ValueError("command is an argv list, not a shell string")
    for mount in value.get("mounts", []):
        if (not isinstance(mount, dict) or not isinstance(mount.get("source"), str) or
            not isinstance(mount.get("target"), str) or not os.path.isabs(mount["source"]) or
            not os.path.isabs(mount["target"])):
            raise ValueError("Mount source and target must be absolute")
        if not mount.get("read_only", True) and not mount.get("cache_only", False):
            raise ValueError("Writable local mounts require explicit disposable cache_only")
    if value.get("restart_policy") not in (None, "no"):
        raise ValueError("Only the grant-aware runner may restart the service")
    for key in ("lease_ttl_s", "worker_ttl_s", "startup_grace_s"):
        if key in value and (isinstance(value[key], bool) or not isinstance(value[key], (int, float)) or not math.isfinite(value[key]) or value[key] <= 0):
            raise ValueError(key + " must be positive")
    return value


def normalize_platform(os_name, arch):
    arch = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64",
            "amd64": "amd64", "armv7l": "arm/v7", "arm": "arm/v7"}.get(arch, arch)
    return os_name + "/" + arch


class DockerError(RuntimeError):
    pass


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=4):
        http.client.HTTPConnection.__init__(self, "localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix_path)


class DockerEngine:
    """Docker Engine API 1.24 works with the existing 17.06 and 24.x engines."""
    def __init__(self, socket_path="/var/run/docker.sock", timeout=4):
        self.socket_path, self.timeout = socket_path, timeout

    def request(self, method, path, value=None, timeout="default", stream=False):
        conn = UnixConnection(self.socket_path, self.timeout if timeout == "default" else timeout)
        headers = {"Content-Type": "application/json"}
        data = None if value is None else canonical(value).encode("utf-8")
        try:
            conn.request(method, path, body=data, headers=headers)
            response = conn.getresponse()
            if stream and response.status < 300:
                while True:
                    line = response.readline()
                    if not line:
                        break
                    event = json.loads(line.decode("utf-8"))
                    if event.get("error"):
                        raise DockerError(event["error"])
                return {}
            raw = response.read()
            result = json.loads(raw.decode("utf-8")) if raw else {}
            if response.status >= 300:
                error = DockerError("Docker HTTP %s: %s" % (response.status, result.get("message", "")))
                error.status = response.status
                raise error
            return result
        finally:
            conn.close()

    def version(self):
        return self.request("GET", "/version")

    def image(self, image_ref):
        try:
            return self.request("GET", "/v1.24/images/" + quote(image_ref, safe="") + "/json")
        except DockerError as error:
            if getattr(error, "status", None) == 404:
                return None
            raise

    def pull(self, image_ref):
        if image_ref.startswith("sha256:"):
            raise DockerError("A local immutable image id must already exist on this host")
        self.request("POST", "/v1.24/images/create?" + urlencode({"fromImage": image_ref}),
                     timeout=None, stream=True)
        if self.image(image_ref) is None:
            raise DockerError("Pull completed without the requested digest image")

    def containers(self, worker_id):
        filters = canonical({"label": [MANAGED + "=true", WORKER + "=" + worker_id]})
        return self.request("GET", "/v1.24/containers/json?all=1&" + urlencode({"filters": filters}))

    def inspect(self, container):
        try:
            return self.request("GET", "/v1.24/containers/" + quote(container, safe="") + "/json")
        except DockerError as error:
            if getattr(error, "status", None) == 404:
                return None
            raise

    def remove_managed(self, container, worker_id):
        current = self.inspect(container)
        if current is None:
            return
        labels = current.get("Config", {}).get("Labels", {})
        if labels.get(MANAGED) != "true" or labels.get(WORKER) != worker_id:
            raise DockerError("Refusing to stop a container outside this worker's managed scope")
        if current.get("State", {}).get("Running"):
            self.request("POST", "/v1.24/containers/" + container + "/kill?signal=SIGKILL")
        self.request("DELETE", "/v1.24/containers/" + container)

    def create(self, manifest, image_ref, worker_id, incarnation, grant, binding_overrides=None):
        labels = {MANAGED: "true", WORKER: worker_id, SERVICE: manifest["id"],
                  GRANT: grant["grant_token"], "org.biomerte.service.revision": manifest["revision"]}
        environment = dict(manifest.get("environment", {}))
        environment.update({
            "BIOME_SERVICE_ID": manifest["id"],
            "BIOME_SERVICE_REVISION": manifest["revision"],
            "BIOME_SERVICE_BOOT_EPOCH": grant["boot_epoch"],
            "BIOME_SERVICE_GRANT": grant["grant_token"],
            "BIOME_SERVICE_WORKER": worker_id,
            "BIOME_SERVICE_INCARNATION": incarnation})
        host = {"RestartPolicy": {"Name": "no"},
                "LogConfig": {"Type": "json-file", "Config": {
                    "max-size": str(manifest.get("log_max_size", "8m")),
                    "max-file": str(manifest.get("log_max_files", 3))}},
                "Binds": [m["source"] + ":" + m["target"] + (":ro" if m.get("read_only", True) else ":rw")
                          for m in manifest.get("mounts", [])]}
        host["NetworkMode"] = manifest.get("network", "bridge")
        if manifest.get("extra_hosts"):
            host["ExtraHosts"] = list(manifest["extra_hosts"])
        if manifest.get("memory_bytes"):
            host["Memory"] = int(manifest["memory_bytes"])
        if manifest.get("read_only_root"):
            host["ReadonlyRootfs"] = True
        ports = {}
        for item in manifest.get("ports", []):
            key = str(int(item["container"])) + "/" + item.get("protocol", "tcp")
            ports.setdefault(key, []).append({"HostIp": item.get("listen", "127.0.0.1"),
                                             "HostPort": str(int(item["host"]))})
        if binding_overrides:
            for key, val in binding_overrides.items():
                ports[key] = val
        if ports:
            host["PortBindings"] = ports
        config = {"Image": image_ref, "Labels": labels, "HostConfig": host,
                  "Env": [str(k) + "=" + str(v) for k, v in sorted(environment.items())],
                  "ExposedPorts": {k: {} for k in ports}}
        if "command" in manifest:
            config["Cmd"] = manifest["command"]
        if manifest.get("entrypoint"):
            config["Entrypoint"] = manifest["entrypoint"]
        if manifest.get("working_dir"):
            config["WorkingDir"] = manifest["working_dir"]
        health = manifest.get("health")
        if health:
            config["Healthcheck"] = {"Test": ["CMD"] + health["command"],
                "Interval": int(health.get("interval_s", 2) * 1000000000),
                "Timeout": int(health.get("timeout_s", 1) * 1000000000),
                "Retries": int(health.get("retries", 2))}
        name = "biome-" + manifest["id"] + "-" + incarnation[:8] + "-" + grant["grant_token"][:8]
        created = self.request("POST", "/v1.24/containers/create?" + urlencode({"name": name}), config)
        try:
            self.request("POST", "/v1.24/containers/" + created["Id"] + "/start")
        except Exception as error:
            try:
                self.remove_managed(created["Id"], worker_id)
            except Exception:
                # The caller retains custody when cleanup cannot be established.
                error.container_id = created["Id"]
            raise
        return created["Id"]


class CoordinatorClient:
    def __init__(self, origin, key_file, timeout=3, allow_http_for_testing=False):
        parsed = urlsplit(origin)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Coordinator origin must not contain credentials or query")
        if parsed.scheme != "https" and not (allow_http_for_testing and parsed.scheme == "http"):
            raise ValueError("An authenticated HTTPS coordinator is required")
        self.origin = origin.rstrip("/")
        self.key_file, self.timeout = key_file, timeout

    def call(self, operation, value):
        with open(self.key_file) as source:
            key = source.read().strip()
        if len(key) < 32:
            raise ValueError("Coordinator credential is missing")
        data = None if operation in ("status", "catalog") else canonical(value).encode("utf-8")
        req = Request(self.origin + "/api/service-coordinator/" + operation, data,
                      {"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            # Do not put a response body or bearer credential into service logs.
            raise RuntimeError("Coordinator rejected %s with HTTP %s" % (operation, error.code))


class State:
    """Overwrite current observations in local SQLite; no permanent shared journal."""
    def __init__(self, path):
        folder = os.path.dirname(os.path.abspath(path))
        if not os.path.isdir(folder):
            os.makedirs(folder)
        self.path = path
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS services (id TEXT PRIMARY KEY, revision TEXT, state TEXT, observation TEXT, updated REAL)")
            db.execute("CREATE TABLE IF NOT EXISTS manifests (id TEXT PRIMARY KEY, revision TEXT, manifest TEXT)")

    def manifests(self):
        with sqlite3.connect(self.path) as db:
            return {row[0]: json.loads(row[1]) for row in db.execute("SELECT id,manifest FROM manifests")}

    def save_manifest(self, manifest):
        validate_manifest(manifest)
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT OR REPLACE INTO manifests VALUES (?,?,?)",
                       (manifest["id"], manifest["revision"], canonical(manifest)))

    def save(self, service_id, manifest_revision, phase, observation):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT OR REPLACE INTO services VALUES (?,?,?,?,?)",
                       (service_id, manifest_revision, phase, canonical(observation), time.time()))


class Runner:
    def __init__(self, config, engine=None, client=None, clock=None):
        self.config = config
        for name, default in (("heartbeat_interval_s", 1), ("request_timeout_s", 3), ("image_retry_s", 10)):
            value = config.get(name, default)
            if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value) or value <= 0):
                raise ValueError(name + " must be positive and finite")
        self.worker_id = config["worker_id"]
        if not ID.match(self.worker_id):
            raise ValueError("Invalid worker_id")
        self.incarnation = config.get("test_incarnation") or uuid.uuid4().hex
        self.engine = engine or DockerEngine(config.get("docker_socket", "/var/run/docker.sock"))
        self.client = client or CoordinatorClient(config["coordinator_url"], config["coordinator_key_file"],
                        config.get("request_timeout_s", 3), config.get("allow_http_for_testing", False))
        self.clock = clock or time.monotonic
        self.state = State(config["state_file"])
        self.lock = threading.RLock()
        self.grants = {}
        self.containers = {}
        self.pulls = {}
        self.fence_jobs = {}
        self.fence_acks = {}
        self.manifests = {}
        self.prepared = {}
        self.stop_event = threading.Event()
        self.platform = None
        self.cleaned = False

    def load_manifests(self):
        manifests = self.state.manifests()
        seen = set()
        for path in self.config.get("manifest_files", []):
            with open(path) as source:
                value = validate_manifest(json.load(source))
            if value["id"] in seen:
                raise ValueError("Duplicate service id in initial manifest files")
            seen.add(value["id"])
            if value["id"] not in manifests:
                self.state.save_manifest(value)
                manifests[value["id"]] = value
        self.manifests = manifests

    def adopt_manifests(self, response):
        configured = response.get("configured_revisions", {})
        for value in response.get("manifests", []):
            value = validate_manifest(value)
            old = self.manifests.get(value["id"])
            if old is None or old["revision"] == value["revision"] or configured.get(value["id"]) == value["revision"]:
                if old is None or old["revision"] != value["revision"]:
                    self.state.save_manifest(value)
                    self.manifests[value["id"]] = value
                    # New revision is not eligible until exact-image preparation.
                    self.prepared[value["id"]] = False

    def _pull(self, image_ref):
        try:
            self.engine.pull(image_ref)
            self.pulls[image_ref] = None
        except Exception as error:
            self.pulls[image_ref] = {"error": str(error),
                "retry_at": self.clock() + self.config.get("image_retry_s", 10)}
            LOG.warning("Image preparation failed: %s", error)

    def _dependencies(self, manifest):
        for dep in manifest.get("readiness", []):
            try:
                with urlopen(dep["url"], timeout=dep.get("timeout_s", 2)) as response:
                    if response.getcode() != dep.get("status", 200):
                        return False
            except Exception:
                return False
        return True

    def _health(self, service_id):
        container = self.containers.get(service_id)
        if not container:
            return None
        info = self.engine.inspect(container)
        if not info or not info.get("State", {}).get("Running"):
            return False
        health = info["State"].get("Health", {}).get("Status")
        if health == "starting":
            return None
        return health == "healthy" if health else True

    def advertisements(self):
        version = self.engine.version()
        self.platform = normalize_platform(version.get("Os", "linux"), version["Arch"])
        if not self.cleaned:
            # A new supervisor incarnation must not inherit orphaned authority.
            for container in self.engine.containers(self.worker_id):
                self.engine.remove_managed(container["Id"], self.worker_id)
            self.cleaned = True
        result = []
        for service_id, manifest in sorted(self.manifests.items()):
            image_ref = manifest["images"].get(self.platform)
            image = self.engine.image(image_ref) if image_ref else None
            image_platform = normalize_platform(image.get("Os", ""), image.get("Architecture", "")) if image else None
            prepared = bool(image and image_platform == self.platform)
            pull_state = self.pulls.get(image_ref)
            retry_due = isinstance(pull_state, dict) and self.clock() >= pull_state["retry_at"]
            if image_ref and image is None and (image_ref not in self.pulls or retry_due):
                thread = threading.Thread(target=self._pull, args=(image_ref,))
                thread.daemon = True
                self.pulls[image_ref] = thread
                thread.start()
            dependencies = prepared and self._dependencies(manifest)
            self.prepared[service_id] = bool(prepared and dependencies)
            current = self.grants.get(service_id, {})
            entry = {"manifest": manifest, "manifest_revision": manifest["revision"],
                     "platform": self.platform, "image_ref": image_ref,
                     "prepared": prepared, "dependencies_ready": bool(dependencies),
                     "healthy": self._health(service_id)}
            if current:
                entry.update({"grant_token": current["grant_token"], "boot_epoch": current["boot_epoch"]})
            if service_id in self.fence_acks:
                entry["fence_ack"] = self.fence_acks[service_id]
            result.append(entry)
        return result

    def _fence(self, service_id, grant):
        manifest = self.manifests[service_id]
        hook = manifest["fence_hook"]
        try:
            parsed = urlsplit(hook["url"])
            if parsed.scheme != "https" and not self.config.get("allow_http_for_testing"):
                raise ValueError("Fence acknowledgement requires HTTPS")
            headers = {"Content-Type": "application/json"}
            if hook.get("token_file"):
                with open(hook["token_file"]) as source:
                    headers["Authorization"] = "Bearer " + source.read().strip()
            payload = {"service_id": service_id, "worker_id": self.worker_id,
                       "worker_incarnation": self.incarnation,
                       "manifest_revision": manifest["revision"],
                       "boot_epoch": grant["boot_epoch"], "grant_token": grant["grant_token"]}
            req = Request(hook["url"], canonical(payload).encode("utf-8"), headers)
            with urlopen(req, timeout=hook.get("timeout_s", 3)) as response:
                ack = json.loads(response.read().decode("utf-8"))
            if ack.get("fenced") is not True or ack.get("grant_token") != grant["grant_token"] or ack.get("boot_epoch") != grant["boot_epoch"]:
                raise ValueError("Fence hook did not acknowledge this exact grant")
            with self.lock:
                current = self.grants.get(service_id, {})
                if current.get("grant_token") == grant["grant_token"] and self.clock() < current["deadline"]:
                    self.fence_acks[service_id] = ack
        except Exception as error:
            LOG.warning("Service %s remains fenced: %s", service_id, error)
        finally:
            self.fence_jobs.pop(service_id, None)

    def stop_service(self, service_id, expired_only=False):
        # Serialize removal with renewal so an expiry snapshot cannot kill a
        # grant whose deadline was renewed before the watchdog acquired custody.
        with self.lock:
            grant = self.grants.get(service_id)
            if expired_only and (not grant or self.clock() < grant["deadline"]):
                return True
            container = self.containers.get(service_id)
            if container:
                try:
                    self.engine.remove_managed(container, self.worker_id)
                except Exception as error:
                    LOG.error("Service %s stop unresolved: %s", service_id, error)
                    return False
            self.containers.pop(service_id, None)
            self.grants.pop(service_id, None)
            self.fence_acks.pop(service_id, None)
        return True

    def expire(self):
        with self.lock:
            expired = [sid for sid, grant in self.grants.items() if self.clock() >= grant["deadline"]]
        for service_id in expired:
            self.stop_service(service_id, expired_only=True)

    def accept(self, response, request_started):
        assignments = response.get("assignments", [])
        if isinstance(assignments, dict):
            assignments = list(assignments.values())
        received = {}
        for grant in assignments:
            service_id = grant.get("service_id")
            manifest = self.manifests.get(service_id)
            if (not manifest or grant.get("worker_id") != self.worker_id or
                grant.get("worker_incarnation") != self.incarnation or
                grant.get("boot_epoch") != response.get("boot_epoch") or
                grant.get("manifest_revision") != manifest["revision"] or
                not isinstance(grant.get("grant_token"), str) or not grant.get("grant_token") or
                not isinstance(grant.get("boot_epoch"), str) or not grant.get("boot_epoch")):
                continue
            ttl = grant.get("lease_ttl_s", 0)
            if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or not math.isfinite(ttl) or ttl <= 0:
                continue
            if ttl > manifest.get("lease_ttl_s", 10):
                raise ValueError("Coordinator returned a lease longer than the manifest")
            current = dict(grant)
            current["deadline"] = request_started + ttl
            if self.clock() >= current["deadline"]:
                continue
            received[service_id] = current
        with self.lock:
            old = dict(self.grants)
        for sid, grant in old.items():
            new = received.get(sid)
            if not new or new["grant_token"] != grant["grant_token"] or new["boot_epoch"] != grant["boot_epoch"]:
                if not self.stop_service(sid):
                    received.pop(sid, None)
        for sid, grant in received.items():
            with self.lock:
                self.grants[sid] = grant
            manifest = self.manifests[sid]
            phase = grant.get("state", "active")
            if manifest["effect_policy"] != "stateless":
                ack = self.fence_acks.get(sid, {})
                if ack.get("grant_token") != grant["grant_token"] or ack.get("boot_epoch") != grant["boot_epoch"]:
                    if sid not in self.fence_jobs:
                        thread = threading.Thread(target=self._fence, args=(sid, grant))
                        thread.daemon = True
                        self.fence_jobs[sid] = thread
                        thread.start()
                    continue
                if phase not in ("active", "starting") or grant.get("effect_ready") is not True:
                    continue
            if phase not in ("active", "starting"):
                continue
            if not self.prepared.get(sid) or self.clock() >= grant["deadline"]:
                continue
            if sid not in self.containers:
                try:
                    container = self.engine.create(manifest, manifest["images"][self.platform],
                                                   self.worker_id, self.incarnation, grant,
                                                   self.config.get("port_overrides", {}).get(sid))
                    with self.lock:
                        current = self.grants.get(sid, {})
                        if (self.clock() >= grant["deadline"] or
                            current.get("grant_token") != grant["grant_token"] or
                            current.get("boot_epoch") != grant["boot_epoch"]):
                            self.engine.remove_managed(container, self.worker_id)
                        else:
                            self.containers[sid] = container
                except Exception as error:
                    if getattr(error, "container_id", None):
                        with self.lock:
                            self.containers[sid] = error.container_id
                    LOG.error("Service %s start failed: %s", sid, error)
            self.state.save(sid, manifest["revision"], phase, {
                "worker_id": self.worker_id, "incarnation": self.incarnation,
                "platform": self.platform, "container": self.containers.get(sid),
                "healthy": self._health(sid), "boot_epoch": grant["boot_epoch"]})
        self.expire()

    def tick(self):
        self.load_manifests()
        advertisements = self.advertisements()
        request_started = self.clock()
        response = self.client.call("heartbeat", {"worker_id": self.worker_id,
                   "worker_incarnation": self.incarnation, "services": advertisements})
        self.adopt_manifests(response)
        self.accept(response, request_started)
        return response

    def watchdog(self):
        while not self.stop_event.wait(0.2):
            try:
                self.expire()
            except Exception:
                LOG.exception("Expiry check failed")

    def run(self):
        watchdog = threading.Thread(target=self.watchdog)
        watchdog.daemon = True
        watchdog.start()
        try:
            while not self.stop_event.is_set():
                started = self.clock()
                try:
                    self.tick()
                except Exception as error:
                    LOG.warning("Reconciliation unavailable: %s", error)
                    self.expire()
                interval = self.config.get("heartbeat_interval_s", 1)
                self.stop_event.wait(max(0, interval - (self.clock() - started)))
        finally:
            self.stop_event.set()
            for service_id in list(self.grants):
                self.stop_service(service_id)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true", help="One reconciliation; for qualification only")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open(args.config) as source:
        config = json.load(source)
    runner = Runner(config)
    if args.once:
        try:
            runner.tick()
        finally:
            for service_id in list(runner.grants):
                runner.stop_service(service_id)
    else:
        signal.signal(signal.SIGTERM, lambda *_: runner.stop_event.set())
        signal.signal(signal.SIGINT, lambda *_: runner.stop_event.set())
        runner.run()


if __name__ == "__main__":
    main()
