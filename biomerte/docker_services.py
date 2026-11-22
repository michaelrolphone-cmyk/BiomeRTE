"""Declarative Docker services under an independent, ephemeral placement witness.

The witness grants placement, not arbitrary physical authority. Stateless jobs
must tolerate duplicate execution during a paused host. Effectful services need
a real sink fence acknowledged before their command is started. Existing Biome
capability leases and explicit ownership transfer are never changed here.
"""
from __future__ import print_function
import argparse
import base64
import calendar
import datetime
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
import stat
import threading
import time
import uuid
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError
try:
    from . import service_adapters as adapters
except ImportError:  # The minimal supervisor image executes this module directly.
    import service_adapters as adapters

LOG = logging.getLogger("biomerte.docker_services")
PIN = re.compile(r"^(?:[^\s]+@)?sha256:[a-f0-9]{64}$")
ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}$")
MANAGED = "org.biomerte.service.managed"
WORKER = "org.biomerte.service.worker"
SERVICE = "org.biomerte.service.id"
GRANT = "org.biomerte.service.grant"
LEASE = "org.biomerte.service.capability-lease"
GENERATION = "org.biomerte.service.capability-generation"
INVENTORY_INTERVAL_S = 15


def container_name(manifest, worker_id, incarnation, grant):
    # The same full binding always has the same name, including after a lost reply.
    identity = [manifest["id"], manifest["revision"], worker_id, incarnation,
                grant["boot_epoch"], grant["grant_token"]]
    if grant.get("capability_demand"):
        identity.append(grant["capability_demand"])
    digest = hashlib.sha256(canonical(identity).encode("utf-8")).hexdigest()
    return "biome-" + manifest["id"] + "-" + digest[:24]


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
    state = value.get("state_policy")
    if state not in ("stateless", "externally_managed", "lease_scoped"):
        raise ValueError("Local-only authoritative state cannot automatically fail over")
    demand = value.get("capability_lease")
    if (demand is not None) != (state == "lease_scoped"):
        raise ValueError("Capability demand requires the lease_scoped state policy")
    if demand is not None and (not isinstance(demand, dict) or
            set(demand) != {"controller", "provider", "resource", "contract"} or
            any(not isinstance(v, str) or not 0 < len(v) <= 192 for v in demand.values()) or
            not all(adapters.DIGEST.fullmatch(demand[k]) for k in ("controller", "provider")) or
            not isinstance(value.get("health"), dict) or
            not isinstance(value["health"].get("command"), list) or not value["health"]["command"] or
            any(not isinstance(arg, str) for arg in value["health"]["command"]) or
            value.get("activation") is not None or value.get("authority") is not None):
        raise ValueError("Lease-scoped workers require exact capability scope and a health command")
    if effect != "stateless" and not isinstance(value.get("fence_hook"), dict):
        raise ValueError("Effectful services require an actual fence hook")
    if value.get("fence_hook") and not value["fence_hook"].get("url"):
        raise ValueError("Fence hook URL is required")
    if "command" in value and (not isinstance(value["command"], list) or
                              any(not isinstance(x, str) for x in value["command"])):
        raise ValueError("command is an argv list, not a shell string")
    adapters.validate(value)
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


def docker_timestamp(value):
    """Parse Docker RFC3339Nano timestamps without losing probe ordering."""
    match = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|([+-])(\d{2}):(\d{2}))$",
                     value) if isinstance(value, str) else None
    if not match:
        raise ValueError("Invalid Docker timestamp")
    seconds = calendar.timegm(datetime.datetime.strptime(
        match.group(1), "%Y-%m-%dT%H:%M:%S").timetuple())
    if match.group(3) != "Z":
        hours, minutes = int(match.group(5)), int(match.group(6))
        if hours > 23 or minutes > 59:
            raise ValueError("Invalid Docker timestamp offset")
        offset = (hours * 60 + minutes) * 60
        seconds -= offset if match.group(4) == "+" else -offset
    return seconds * 1000000000 + int((match.group(2) or "").ljust(9, "0"))


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
    def __init__(self, socket_path="/var/run/docker.sock", timeout=4, operation_timeout=30,
                 registry_auth_file=None):
        if registry_auth_file is not None and (not isinstance(registry_auth_file, str) or not os.path.isabs(registry_auth_file)):
            raise ValueError("registry_auth_file must be an absolute private file path")
        self.socket_path, self.timeout = socket_path, timeout
        self.operation_timeout = operation_timeout
        self.registry_auth_file = registry_auth_file
        self.api_prefix = "/v1.24/"

    def request(self, method, path, value=None, timeout="default", stream=False, registry_auth=None):
        conn = UnixConnection(self.socket_path, self.timeout if timeout == "default" else timeout)
        headers = {"Content-Type": "application/json"}
        if registry_auth is not None:
            if method != "POST" or not path.startswith("/v1.24/images/create?"):
                raise DockerError("Registry authentication is only valid for image pulls")
            headers["X-Registry-Auth"] = registry_auth
        data = None if value is None else canonical(value).encode("utf-8")
        try:
            if path.startswith("/v1.24/"):
                path = self.api_prefix + path[len("/v1.24/"):]
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
        info = self.request("GET", "/version")
        minimum = info.get("MinAPIVersion", "1.24")
        if re.fullmatch(r"[0-9]+\.[0-9]+", minimum) and tuple(map(int, minimum.split("."))) > (1, 24):
            self.api_prefix = "/v" + minimum + "/"
        return info

    def daemon_time(self):
        return docker_timestamp(self.request("GET", "/v1.24/info").get("SystemTime"))

    def image(self, image_ref):
        try:
            return self.request("GET", "/v1.24/images/" + quote(image_ref, safe="") + "/json")
        except DockerError as error:
            if getattr(error, "status", None) == 404:
                return None
            raise


    def _registry_auth_header(self, image_ref):
        path = getattr(self, "registry_auth_file", None)
        if path is None:
            return None
        descriptor = None
        try:
            # A directory bind makes atomic host-side file rotation visible.
            # Refuse symlinks and nonregular files before reading any bytes.
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) not in (0o400, 0o600):
                raise ValueError("Unsafe credential file")
            with os.fdopen(descriptor, "r", encoding="utf-8") as source:
                descriptor = None
                config = json.load(source)
            if not isinstance(config, dict) or set(config) != {"auths"} or not isinstance(config["auths"], dict):
                raise ValueError("Expected a dedicated Docker auth file")
            auths = config["auths"]
            if any(not isinstance(key, str) or not re.match(r"^[a-z0-9][a-z0-9.-]*(?::[0-9]+)?\Z", key)
                   or not isinstance(value, dict) for key, value in auths.items()):
                raise ValueError("Expected exact registry authority keys")
            name = image_ref.split("@", 1)[0]
            first = name.split("/", 1)[0]
            explicit = "/" in name and ("." in first or ":" in first or first == "localhost" or first.lower() != first)
            registry = first.lower() if explicit else "docker.io"
            credentials = auths.get(registry)
            if credentials is None:
                return None
            if set(credentials) == {"auth"}:
                encoded = credentials["auth"]
                if not isinstance(encoded, str):
                    raise ValueError("Invalid Docker auth")
                decoded = base64.b64decode(encoded.encode("ascii"), validate=True).decode("utf-8")
                username, separator, password = decoded.partition(":")
                if not separator:
                    raise ValueError("Invalid Docker auth")
            elif set(credentials) == {"username", "password"}:
                username, password = credentials["username"], credentials["password"]
            else:
                raise ValueError("Credential helpers and ambiguous auth entries are unsupported")
            if (not isinstance(username, str) or not isinstance(password, str) or not username or not password or
                    any(char in username or char in password for char in ("\x00", "\r", "\n"))):
                raise ValueError("Invalid registry credentials")
            value = {"username": username, "password": password, "serveraddress": registry}
            return base64.urlsafe_b64encode(canonical(value).encode("utf-8")).decode("ascii")
        except Exception:
            # Neither credential bytes nor parser/OS messages enter runner state.
            raise DockerError("Registry authentication file is unavailable or invalid") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def pull(self, image_ref):
        if image_ref.startswith("sha256:"):
            raise DockerError("A local immutable image id must already exist on this host")
        header = self._registry_auth_header(image_ref)
        options = {"timeout": None, "stream": True}
        if header is not None:
            options["registry_auth"] = header
        try:
            self.request("POST", "/v1.24/images/create?" + urlencode({"fromImage": image_ref}), **options)
        except Exception:
            if header is not None:
                raise DockerError("Authenticated registry image pull failed") from None
            raise
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
        # Names can be reused between inspect and delete. Address only the
        # immutable ID that passed the ownership check. Docker's v flag removes
        # anonymous volumes only; named volumes and bind mounts remain owned by callers.
        path = "/v1.24/containers/" + quote(current["Id"], safe="") + "?force=1"
        if LEASE in labels:
            path += "&v=1"
        try:
            self.request("DELETE", path, timeout=getattr(self, "operation_timeout", 30))
        except DockerError as error:
            if getattr(error, "status", None) != 404:
                raise

    def create(self, manifest, image_ref, worker_id, incarnation, grant, binding_overrides=None,
               mount_bindings=None, initial_phase=None, checkpoint=None, adapter_url=None,
               admit=None, on_created=None):
        labels = {MANAGED: "true", WORKER: worker_id, SERVICE: manifest["id"],
                  GRANT: grant["grant_token"], "org.biomerte.service.revision": manifest["revision"],
                  "org.biomerte.service.incarnation": incarnation,
                  "org.biomerte.service.boot_epoch": grant["boot_epoch"]}
        environment = dict(manifest.get("environment", {}))
        environment.update({
            "BIOME_SERVICE_ID": manifest["id"],
            "BIOME_SERVICE_REVISION": manifest["revision"],
            "BIOME_SERVICE_BOOT_EPOCH": grant["boot_epoch"],
            "BIOME_SERVICE_GRANT": grant["grant_token"],
            "BIOME_SERVICE_WORKER": worker_id,
            "BIOME_SERVICE_INCARNATION": incarnation})
        if manifest.get("capability_lease"):
            lease = grant["capability_demand"]
            labels.update({LEASE: lease["id"], GENERATION: str(lease["generation"])})
            environment.update(BIOME_CAPABILITY_LEASE_ID=lease["id"],
                               BIOME_CAPABILITY_LEASE_GENERATION=str(lease["generation"]))
        if initial_phase is not None:
            if initial_phase != "preparing" or not adapters.staged(manifest):
                raise ValueError("Only an explicitly staged service may start in preparation")
            environment["BIOME_SERVICE_PHASE"] = initial_phase
            environment["BIOME_SERVICE_CHECKPOINT"] = checkpoint
            if adapter_url is not None:
                environment["BIOME_SERVICE_ADAPTER_URL"] = adapter_url
        mounts = adapters.resolve_mounts(manifest, mount_bindings or {})
        host = {"RestartPolicy": {"Name": "no"},
                "LogConfig": {"Type": "json-file", "Config": {
                    "max-size": str(manifest.get("log_max_size", "8m")),
                    "max-file": str(manifest.get("log_max_files", 3))}},
                "Binds": [m["source"] + ":" + m["target"] + (":ro" if m.get("read_only", True) else ":rw")
                          for m in mounts]}
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
        name = container_name(manifest, worker_id, incarnation, grant)
        if admit is not None and not admit():
            raise DockerError("Service grant expired before Docker create")
        try:
            created = self.request("POST", "/v1.24/containers/create?" + urlencode({"name": name}),
                                   config, timeout=getattr(self, "operation_timeout", 30))
        except Exception as error:
            # A transport timeout cannot prove whether the named create happened.
            # Do not replay it under another name; retain this exact cleanup reference.
            if getattr(error, "status", None) not in (400, 401, 403, 404, 422):
                error.container_id, error.creation_uncertain = name, True
            raise
        try:
            if on_created is not None:
                on_created(created["Id"])
            if admit is not None and not admit():
                raise DockerError("Service grant expired before Docker start")
            self.request("POST", "/v1.24/containers/" + created["Id"] + "/start",
                         timeout=getattr(self, "operation_timeout", 30))
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
            db.execute("CREATE TABLE IF NOT EXISTS children (service TEXT PRIMARY KEY, container TEXT, uncertain INTEGER)")

    def children(self):
        with sqlite3.connect(self.path) as db:
            return {row[0]: {"container": row[1], "uncertain": bool(row[2])}
                    for row in db.execute("SELECT service,container,uncertain FROM children")}

    def save_child(self, service_id, container, uncertain):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT OR REPLACE INTO children VALUES (?,?,?)",
                       (service_id, container, int(uncertain)))

    def forget_child(self, service_id, container=None):
        with sqlite3.connect(self.path) as db:
            if container is None:
                removed = db.execute("DELETE FROM children WHERE service=?", (service_id,))
            else:
                removed = db.execute("DELETE FROM children WHERE service=? AND container=?",
                                     (service_id, container))
            if removed.rowcount:
                db.execute("UPDATE services SET state='stopped', observation=?, updated=? WHERE id=?",
                           (canonical({"container": None, "healthy": False}), time.time(), service_id))

    def cleanup_pending(self, service_id, container):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE services SET state='cleanup_pending', observation=?, updated=? WHERE id=?",
                       (canonical({"container": container, "healthy": False}), time.time(), service_id))

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
            encoded = canonical(observation)
            db.execute("INSERT OR REPLACE INTO services SELECT ?,?,?,?,? WHERE NOT EXISTS "
                       "(SELECT 1 FROM services WHERE id=? AND revision=? AND state=? AND observation=?)",
                       (service_id, manifest_revision, phase, encoded, time.time(),
                        service_id, manifest_revision, phase, encoded))


class Runner:
    def __init__(self, config, engine=None, client=None, clock=None):
        self.config = config
        for name, default in (("heartbeat_interval_s", 1), ("request_timeout_s", 3), ("image_retry_s", 10),
                              ("docker_request_timeout_s", 30)):
            value = config.get(name, default)
            if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value) or value <= 0):
                raise ValueError(name + " must be positive and finite")
        self.worker_id = config["worker_id"]
        if not ID.match(self.worker_id):
            raise ValueError("Invalid worker_id")
        self.incarnation = config.get("test_incarnation") or uuid.uuid4().hex
        self.engine = engine or DockerEngine(config.get("docker_socket", "/var/run/docker.sock"),
                        operation_timeout=config.get("docker_request_timeout_s", 30),
                        registry_auth_file=config.get("registry_auth_file"))
        self.client = client or CoordinatorClient(config["coordinator_url"], config["coordinator_key_file"],
                        config.get("request_timeout_s", 3), config.get("allow_http_for_testing", False))
        self.clock = clock or time.monotonic
        self.state = State(config["state_file"])
        self.lock = threading.RLock()
        self.grants = {}
        self.containers = {}
        self.start_jobs = {}
        self.start_bindings = {}
        self.starting_refs = {}
        self.pending_cleanup = self.state.children()
        self.cleanup_jobs = {}
        self.pulls = {}
        self.fence_jobs = {}
        self.fence_acks = {}
        self.adapter_jobs = {}
        self.state_proofs = {}
        self.activation_acks = {}
        self.binding_errors = {}
        self.manifests = {}
        self.prepared = {}
        self.stop_event = threading.Event()
        self.platform = None
        self.next_inventory = 0
        self.absent_creates = {}

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
        if service_id in self.pending_cleanup or service_id in self.start_jobs:
            return None
        container = self.containers.get(service_id)
        if not container:
            return None
        info = self.engine.inspect(container)
        if not info or not info.get("State", {}).get("Running"):
            return False
        manifest = self.manifests.get(service_id, {})
        health = info["State"].get("Health") or {}
        status = health.get("Status")
        if adapters.staged(manifest):
            activated = self.activation_acks.get(service_id)
            if not activated:
                return None  # An inert preparation process is not an active service.
            check = (info.get("Config", {}).get("Healthcheck") or {}).get("Test", [])
            if manifest.get("health") or health or (check and check[0] != "NONE"):
                fresh = False
                try:
                    sample = health["Log"][-1]
                    start, end = docker_timestamp(sample["Start"]), docker_timestamp(sample["End"])
                    boundary = activated["health_after"]
                    fresh = (status in ("healthy", "unhealthy") and boundary is not None and
                             start > boundary and end >= start and
                             activated["health_container"] == container)
                except (KeyError, IndexError, TypeError, ValueError):
                    pass
                if not fresh or status == "starting":
                    return None if self.clock() < activated["health_deadline"] else False
        if status == "starting":
            return None
        if manifest.get("capability_lease"):
            return status == "healthy"
        return status == "healthy" if status else True

    def _inventory_children(self):
        for item in self.engine.containers(self.worker_id):
            child = item["Id"]
            with self.lock:
                if child in self.containers.values():
                    continue
            info = self.engine.inspect(child)
            if not info:
                continue
            service_id = info.get("Config", {}).get("Labels", {}).get(SERVICE)
            aliases = {child, info.get("Name", "").lstrip("/")}
            with self.lock:
                if child in self.containers.values() or service_id in self.start_jobs:
                    continue
                retained = self.pending_cleanup.get(service_id)
                if retained and retained["container"] in aliases:
                    key = service_id
                else:
                    key = "_orphan:" + (service_id or "") + ":" + child
                if self.pending_cleanup.get(key, {}).get("container") == child:
                    continue
            self._queue_cleanup(key, child)

    def advertisements(self):
        version = self.engine.version()
        self.platform = normalize_platform(version.get("Os", "linux"), version["Arch"])
        if self.clock() >= self.next_inventory:
            self._inventory_children()
            self.next_inventory = self.clock() + INVENTORY_INTERVAL_S
        for service_id in list(self.pending_cleanup):
            self._begin_cleanup(service_id)
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
            dependencies = (prepared and not self._has_orphans(service_id) and
                            service_id not in self.pending_cleanup and
                            self._bindings_ready(manifest) and self._dependencies(manifest))
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
            if adapters.staged(manifest):
                proof = self.state_proofs.get(service_id)
                state_ready = adapters.proof_matches(proof, current)
                entry.update(state_ready=state_ready, state_proof=proof if state_ready else None)
            result.append(entry)
        return result


    def _has_orphans(self, service_id):
        # Older state files stored only the container ID for unidentified orphans.
        with self.lock:
            return any(key.startswith("_orphan:") and
                       (key.count(":") == 1 or key.startswith(("_orphan::", "_orphan:" + service_id + ":")))
                       for key in self.pending_cleanup)

    def _bindings_ready(self, manifest):
        try:
            adapters.resolve_mounts(manifest, self.config.get("bindings", {}))
            if adapters.staged(manifest):
                adapters.AdapterClient(self.config, manifest)
            self.binding_errors.pop(manifest["id"], None)
            return True
        except (ValueError, TypeError, AttributeError) as error:
            reason = str(error)
            if self.binding_errors.get(manifest["id"]) != reason:
                LOG.warning("Service %s host bindings unavailable: %s", manifest["id"], reason)
            self.binding_errors[manifest["id"]] = reason
            return False

    def _current(self, grant):
        try:
            current = self.grants.get(grant["service_id"], {})
            manifest = self.manifests.get(grant["service_id"], {})
            return (not self.stop_event.is_set() and
                    adapters.binding(current) == adapters.binding(grant) and
                    current.get("capability_demand") == grant.get("capability_demand") and
                    manifest.get("revision") == grant["manifest_revision"] and
                    self.clock() < current["deadline"])
        except (ValueError, KeyError):
            return False

    def _adapter_job(self, service_id, grant, operation, job_key):
        try:
            with self.lock:
                if not self._current(grant):
                    return
                manifest = self.manifests[service_id]
                if operation == "activate":
                    container = self.containers.get(service_id)
                    if not container:
                        return
                    proof = self.state_proofs.get(service_id)
                    ack = self.fence_acks.get(service_id)
                    if (not adapters.fence_matches(ack, grant, proof) or
                            grant.get("effect_ready") is not True or
                            grant.get("state") not in ("starting", "active")):
                        return
                    body = adapters.request(operation, grant,
                                            checkpoint=proof["checkpoint"], fence_ack=ack)
                else:
                    body = adapters.request(operation, grant)
            client = adapters.AdapterClient(self.config, manifest)
            result = client.call(body)
            proof = (adapters.prepared(body, result) if operation == "prepare"
                     else adapters.activated(body, result))
            if operation == "activate":
                # Read after the ACK, in Docker's clock domain. This I/O must not
                # hold the grant lock or reset the coordinator's startup clock.
                proof["health_deadline"] = self.clock() + manifest.get("startup_grace_s", 20)
                proof["health_container"] = container
                try:
                    proof["health_after"] = self.engine.daemon_time()
                except Exception as error:
                    proof["health_after"] = None
                    LOG.warning("Service %s health activation boundary unavailable (%s)",
                                service_id, type(error).__name__)
            with self.lock:
                if self._current(grant):
                    if operation == "prepare":
                        self.state_proofs[service_id] = proof
                    else:
                        current_proof = self.state_proofs.get(service_id)
                        current = self.grants[service_id]
                        if (adapters.proof_matches(current_proof, grant) and
                                self.containers.get(service_id) == container and
                                current.get("effect_ready") is True and
                                current.get("state") in ("starting", "active") and
                                current_proof["checkpoint"] == proof["checkpoint"]):
                            self.activation_acks[service_id] = proof
        except Exception as error:
            # Adapter URLs, credentials, and arbitrary exception bodies are not logged.
            LOG.warning("Service %s %s remains pending (%s)",
                        service_id, operation, type(error).__name__)
        finally:
            with self.lock:
                self.adapter_jobs.pop(job_key, None)

    def _begin_adapter(self, service_id, grant, operation):
        # A new binding never waits for an unrelated, slow prior incarnation.
        identity = adapters.request(operation, grant)["id"]
        job_key = (service_id, operation, identity)
        with self.lock:
            if job_key in self.adapter_jobs or not self._current(grant):
                return
            thread = threading.Thread(target=self._adapter_job,
                                      args=(service_id, dict(grant), operation, job_key))
            thread.daemon = True
            self.adapter_jobs[job_key] = thread
        try:
            thread.start()
        except Exception:
            with self.lock:
                self.adapter_jobs.pop(job_key, None)
            raise

    def _admit_start(self, service_id, grant, staging):
        with self.lock:
            if (not self._current(grant) or not self.prepared.get(service_id) or
                    service_id in self.pending_cleanup or self._has_orphans(service_id)):
                return False
            current = self.grants[service_id]
            if staging:
                return (current.get("state_ready") is True and
                        adapters.proof_matches(self.state_proofs.get(service_id), current))
            if current.get("state", "active") not in ("starting", "active"):
                return False
            return (self.manifests[service_id]["effect_policy"] == "stateless" or
                    current.get("effect_ready") is True)

    def _queue_cleanup(self, service_id, container, uncertain=False):
        record = {"container": container, "uncertain": uncertain}
        with self.lock:
            self.pending_cleanup[service_id] = record
        try:
            self.state.save_child(service_id, container, uncertain)
            self.state.cleanup_pending(service_id, container)
        except Exception as error:
            # Keep memory custody even when the local disk cannot accept a record.
            LOG.error("Service %s cleanup persistence failed (%s)", service_id, type(error).__name__)
        self._begin_cleanup(service_id)

    def _cleanup_child(self, service_id, record):
        try:
            if record["uncertain"] and self.engine.inspect(record["container"]) is None:
                # A create with no reply has no ID to start. While its caller
                # is still running keep the barrier. Once settled, allow a
                # full observation interval for a late create. Inventory keeps
                # collecting any later inert child after this reservation clears.
                with self.lock:
                    if service_id in self.start_jobs:
                        return
                    first = self.absent_creates.setdefault(record["container"], self.clock())
                    if self.clock() - first < INVENTORY_INTERVAL_S:
                        return
            else:
                self.engine.remove_managed(record["container"], self.worker_id)
            self.state.forget_child(service_id, record["container"])
            with self.lock:
                self.absent_creates.pop(record["container"], None)
                if self.pending_cleanup.get(service_id) == record:
                    self.pending_cleanup.pop(service_id, None)
        except Exception as error:
            LOG.warning("Service %s cleanup remains pending (%s)", service_id, type(error).__name__)
        finally:
            with self.lock:
                self.cleanup_jobs.pop(service_id, None)

    def _begin_cleanup(self, service_id):
        with self.lock:
            record = self.pending_cleanup.get(service_id)
            if not record or service_id in self.cleanup_jobs:
                return
            thread = threading.Thread(target=self._cleanup_child,
                                      args=(service_id, dict(record)))
            thread.daemon = True
            self.cleanup_jobs[service_id] = thread
        try:
            thread.start()
        except Exception:
            with self.lock:
                self.cleanup_jobs.pop(service_id, None)
            raise

    def _created_child(self, service_id, grant, container):
        record = {"container": container, "uncertain": False}
        self.state.save_child(service_id, container, False)
        with self.lock:
            self.starting_refs[service_id] = record
            if service_id in self.pending_cleanup or not self._current(grant):
                self.pending_cleanup[service_id] = record
        if service_id in self.pending_cleanup:
            self._begin_cleanup(service_id)

    def _create_child(self, service_id, grant, staging, manifest, extra):
        container = None
        try:
            with self.lock:
                if not self._admit_start(service_id, grant, staging):
                    return
                name = container_name(manifest, self.worker_id, self.incarnation, grant)
                self.starting_refs[service_id] = {"container": name, "uncertain": True}
            # Durability precedes effects, but slow storage does not hold the grant lock.
            self.state.save_child(service_id, name, True)
            container = self.engine.create(
                manifest, manifest["images"][self.platform], self.worker_id,
                self.incarnation, grant,
                self.config.get("port_overrides", {}).get(service_id),
                admit=lambda: self._admit_start(service_id, grant, staging),
                on_created=lambda cid: self._created_child(service_id, grant, cid), **extra)
            self.state.save_child(service_id, container, False)
            with self.lock:
                admitted = self._admit_start(service_id, grant, staging)
                if admitted:
                    self.containers[service_id] = container
            if not admitted:
                self._queue_cleanup(service_id, container)
        except Exception as error:
            if getattr(error, "container_id", None):
                self._queue_cleanup(service_id, error.container_id,
                                    getattr(error, "creation_uncertain", False))
            elif container is not None:
                self._queue_cleanup(service_id, container)
            else:
                # The Engine create method established either no create or completed cleanup.
                try:
                    self.state.forget_child(service_id)
                except Exception as persistence_error:
                    LOG.error("Service %s cleanup record remains (%s)",
                              service_id, type(persistence_error).__name__)
                with self.lock:
                    self.pending_cleanup.pop(service_id, None)
            LOG.error("Service %s start failed: %s", service_id, error)
        finally:
            with self.lock:
                self.starting_refs.pop(service_id, None)
                self.start_jobs.pop(service_id, None)
                self.start_bindings.pop(service_id, None)
            if service_id in self.pending_cleanup:
                self._begin_cleanup(service_id)

    def _start_child(self, service_id, grant, staging=False):
        with self.lock:
            if service_id in self.containers:
                return True
            if service_id in self.start_jobs or not self._admit_start(service_id, grant, staging):
                return False
            manifest = self.manifests[service_id]
            extra = {}
            if staging or any("binding" in m for m in manifest.get("mounts", [])):
                extra["mount_bindings"] = self.config.get("bindings", {})
            if staging:
                proof = self.state_proofs[service_id]
                adapter = adapters.AdapterClient(self.config, manifest)
                extra.update(initial_phase="preparing", checkpoint=proof["checkpoint"],
                             adapter_url=adapter.url)
            thread = threading.Thread(target=self._create_child,
                       args=(service_id, dict(grant), staging, manifest, extra))
            thread.daemon = True
            self.start_jobs[service_id] = thread
            self.start_bindings[service_id] = adapters.binding(grant)
        try:
            thread.start()
        except Exception:
            with self.lock:
                self.start_jobs.pop(service_id, None)
                self.start_bindings.pop(service_id, None)
            raise
        return service_id in self.containers

    def _record_stage(self, service_id, grant, phase):
        proof = self.state_proofs.get(service_id, {})
        self.state.save(service_id, grant["manifest_revision"], phase, {
            "worker_id": self.worker_id, "incarnation": self.incarnation,
            "container": self.containers.get(service_id),
            "checkpoint": proof.get("checkpoint"),
            "state_ready": adapters.proof_matches(proof, grant),
            "boot_epoch": grant["boot_epoch"]})

    def _staged(self, service_id, grant):
        if (service_id in self.pending_cleanup or
                (service_id in self.start_jobs and
                 self.start_bindings.get(service_id) != adapters.binding(grant))):
            return False
        if not self.prepared.get(service_id) or not self._current(grant):
            return False
        proof = self.state_proofs.get(service_id)
        if not adapters.proof_matches(proof, grant):
            self._begin_adapter(service_id, grant, "prepare")
            self._record_stage(service_id, grant, "preparing_state")
            return False
        if grant.get("state_ready") is not True:
            self._record_stage(service_id, grant, "awaiting_state_admission")
            return False
        if not self._start_child(service_id, grant, staging=True):
            return False
        ack = self.fence_acks.get(service_id)
        if not adapters.fence_matches(ack, grant, proof):
            if service_id not in self.fence_jobs:
                thread = threading.Thread(target=self._fence, args=(service_id, dict(grant)))
                thread.daemon = True
                self.fence_jobs[service_id] = thread
                thread.start()
            self._record_stage(service_id, grant, "fencing")
            return False
        if grant.get("state") not in ("starting", "active") or grant.get("effect_ready") is not True:
            self._record_stage(service_id, grant, "awaiting_effect_admission")
            return False
        activated = self.activation_acks.get(service_id)
        if (not isinstance(activated, dict) or activated.get("binding") != proof["binding"] or
                activated.get("checkpoint") != proof["checkpoint"] or activated.get("active") is not True):
            self._begin_adapter(service_id, grant, "activate")
            self._record_stage(service_id, grant, "activating")
            return False
        return True

    def _fence(self, service_id, grant):
        manifest = self.manifests[service_id]
        hook = manifest["fence_hook"]
        try:
            with self.lock:
                if not self._current(grant):
                    return
                state_proof = self.state_proofs.get(service_id)
                if adapters.staged(manifest) and (
                        not adapters.proof_matches(state_proof, grant) or
                        self.grants[service_id].get("state_ready") is not True):
                    return
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
            if adapters.staged(manifest):
                payload["prepared_state"] = state_proof
            req = Request(hook["url"], canonical(payload).encode("utf-8"), headers)
            with urlopen(req, timeout=hook.get("timeout_s", 3)) as response:
                ack = json.loads(response.read().decode("utf-8"))
            if ack.get("fenced") is not True or ack.get("grant_token") != grant["grant_token"] or ack.get("boot_epoch") != grant["boot_epoch"]:
                raise ValueError("Fence hook did not acknowledge this exact grant")
            if adapters.staged(manifest) and not adapters.fence_matches(ack, grant, state_proof):
                raise ValueError("Fence hook did not bind the prepared checkpoint and full service grant")
            with self.lock:
                if self._current(grant):
                    self.fence_acks[service_id] = ack
        except Exception as error:
            LOG.warning("Service %s remains fenced: %s", service_id, error)
        finally:
            self.fence_jobs.pop(service_id, None)

    def stop_service(self, service_id, expired_only=False):
        # Withdraw local admission immediately. Slow Docker removal happens outside
        # the grant lock, so other services' renewal and expiry keep progressing.
        with self.lock:
            grant = self.grants.get(service_id)
            if expired_only and (not grant or self.clock() < grant["deadline"]):
                return True
            container = self.containers.pop(service_id, None)
            record = ({"container": container, "uncertain": False} if container
                      else self.starting_refs.get(service_id))
            self.grants.pop(service_id, None)
            self.fence_acks.pop(service_id, None)
            self.state_proofs.pop(service_id, None)
            self.activation_acks.pop(service_id, None)
            if record:
                self.pending_cleanup[service_id] = dict(record)
        if record:
            self._queue_cleanup(service_id, record["container"], record["uncertain"])
        else:
            self._begin_cleanup(service_id)
        return service_id not in self.pending_cleanup and service_id not in self.start_jobs

    def expire(self):
        with self.lock:
            expired = [sid for sid, grant in self.grants.items() if self.clock() >= grant["deadline"]]
        for service_id in expired:
            self.stop_service(service_id, expired_only=True)
        for service_id in list(self.pending_cleanup):
            self._begin_cleanup(service_id)

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
            if manifest.get("state_policy") == "lease_scoped":
                lease = grant.get("capability_demand")
                if (not isinstance(lease, dict) or not isinstance(lease.get("id"), str) or
                        not adapters.DIGEST.fullmatch(lease["id"]) or
                        type(lease.get("generation")) is not int or lease["generation"] < 1):
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
            if (not new or adapters.binding(new) != adapters.binding(grant) or
                    new.get("capability_demand") != grant.get("capability_demand")):
                if not self.stop_service(sid):
                    received.pop(sid, None)
        for sid, grant in received.items():
            with self.lock:
                self.grants[sid] = grant
            manifest = self.manifests[sid]
            phase = grant.get("state", "active")
            if adapters.staged(manifest):
                if not self._staged(sid, grant):
                    continue
            elif manifest["effect_policy"] != "stateless":
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
            self._start_child(sid, grant)
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
                   "worker_incarnation": self.incarnation, "services": advertisements,
                   "lease_scoped": True})
        self.adopt_manifests(response)
        self.accept(response, request_started)
        return response

    def watchdog(self):
        while not self.stop_event.wait(0.2):
            try:
                self.expire()
            except Exception:
                LOG.exception("Expiry check failed")

    def close(self):
        self.stop_event.set()
        for service_id in list(self.grants):
            self.stop_service(service_id)
        # Let in-flight creates report their exact child and complete cleanup.
        until = time.monotonic() + self.config.get("docker_request_timeout_s", 30) * 2 + 5
        while True:
            with self.lock:
                jobs = list(self.start_jobs.values()) + list(self.cleanup_jobs.values())
            if not jobs or time.monotonic() >= until:
                break
            for job in jobs:
                if hasattr(job, "join"):
                    job.join(max(0, min(.2, until - time.monotonic())))
        if self.pending_cleanup:
            LOG.warning("Unresolved managed child custody retained for next supervisor startup")

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
            self.close()


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
            runner.close()
    else:
        signal.signal(signal.SIGTERM, lambda *_: runner.stop_event.set())
        signal.signal(signal.SIGINT, lambda *_: runner.stop_event.set())
        runner.run()


if __name__ == "__main__":
    main()
