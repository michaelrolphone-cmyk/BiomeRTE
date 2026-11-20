"""Narrow native boundary for registered routes, vision Unix streams and LAN UDP.

The adapter uses the installed DeviceRoutes implementation and existing store.
It is not a controller, a replica, a general shell/HTTP proxy, or a state
authority adapter. Mutating/device operations and UDP ownership are opt-in.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import socket
import ssl
import struct
import subprocess
import threading
import time
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen
import uuid

from .host_io_client import PROTOCOL, MAX_JSON, MAX_FRAME, read_exact

MAC = re.compile(r"^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$")
DEVICE_PATHS = frozenset(("/api/telemetry/ftm", "/api/ahrs/calibration"))

def boot_clock_id(host_id):
    linux = Path("/proc/sys/kernel/random/boot_id")
    if linux.is_file():
        boot = linux.read_text().strip()
    else:
        reply = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.boottime"],
                               capture_output=True, text=True, timeout=3, check=True)
        boot = reply.stdout.strip()
    if not boot:
        raise ValueError("native boot clock identity unavailable")
    return host_id + ":" + hashlib.sha256(boot.encode()).hexdigest()[:24]

def load_routes_module(directory):
    path = Path(directory).resolve() / "device_routes.py"
    spec = importlib.util.spec_from_file_location("biomerte_native_device_routes", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def native_neighbors():
    linux = Path("/proc/net/arp")
    if linux.is_file():
        result = []
        for line in linux.read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 4 and fields[2] == "0x2":
                result.append((str(ipaddress.ip_address(fields[0])), fields[3].lower()))
        return result
    reply = subprocess.run(["/usr/sbin/arp", "-an"], capture_output=True,
                           text=True, timeout=3, check=True)
    result = []
    for line in reply.stdout.splitlines():
        match = re.search(r"\(([^)]+)\)\s+at\s+([0-9a-f:]+)", line, re.I)
        if match:
            try:
                host = str(ipaddress.ip_address(match[1]))
            except ValueError:
                continue
            result.append((host, match[2].lower()))
    return result

class AnchorHub:
    """Own the original host UDP endpoint; retain peer identity and receive time."""
    def __init__(self, settings, clock_id):
        self.settings, self.clock_id = dict(settings), clock_id
        self.lock = threading.Lock()
        self.subscribers = set()
        self.recent = {}
        self.stop = threading.Event()
        self.sequence = 0
        self.sock = None
        self.thread = None

    def start(self):
        if self.sock is not None:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Deliberately no SO_REUSEADDR: a second authoritative collector must fail.
        sock.bind((self.settings.get("listen", "0.0.0.0"),
                   int(self.settings.get("port", 7446))))
        sock.settimeout(.5)
        self.sock = sock
        self.thread = threading.Thread(target=self._receive, name="host-anchor-udp", daemon=True)
        self.thread.start()

    def _receive(self):
        while not self.stop.is_set():
            try:
                payload, peer = self.sock.recvfrom(16385)
                if len(payload) > 16384:
                    continue
            except socket.timeout:
                continue
            except OSError:
                return
            received = time.monotonic()
            with self.lock:
                self.sequence += 1
                self.recent[peer[0]] = received
                self.recent = {k: v for k, v in self.recent.items() if received - v <= 30}
                item = dict(payload=base64.b64encode(payload).decode(), peer=list(peer),
                            received_monotonic=received, clock_id=self.clock_id,
                            sequence=self.sequence)
                for inbox in self.subscribers:
                    try:
                        inbox.put_nowait(item)
                    except queue.Full:
                        # Sequence discontinuity is explicit; consumers discard
                        # partial legacy packets rather than join different bursts.
                        try:
                            inbox.get_nowait()
                        except queue.Empty:
                            pass
                        inbox.put_nowait(dict(item, dropped=1))

    @contextmanager
    def subscribe(self):
        if self.sock is None:
            raise ValueError("anchor transport is not active")
        inbox = queue.Queue(maxsize=128)
        with self.lock:
            if len(self.subscribers) >= 4:
                raise ValueError("anchor subscriber limit reached")
            self.subscribers.add(inbox)
        try:
            yield inbox
        finally:
            with self.lock:
                self.subscribers.discard(inbox)

    def send(self, record, host, port):
        host = str(ipaddress.IPv4Address(host))
        if (self.sock is None or type(port) is not int or
                port != int(self.settings.get("port", 7446))):
            raise ValueError("anchor transport endpoint is not active")
        if (not isinstance(record, dict) or set(record) !=
                {"schema", "bssid", "ssid", "channel", "ftm_responder"} or
                record["schema"] != "rover.tof.responder/1" or
                not MAC.fullmatch(str(record["bssid"]).lower()) or
                not isinstance(record["ssid"], str) or
                not record["ssid"].startswith("C3-TOF-") or len(record["ssid"]) > 32 or
                type(record["channel"]) is not int or not 1 <= record["channel"] <= 14 or
                record["ftm_responder"] is not True):
            raise ValueError("invalid responder announcement")
        with self.lock:
            seen = self.recent.get(host)
        if seen is None or not 0 <= time.monotonic() - seen <= 30:
            raise ValueError("responder destination has no fresh native UDP observation")
        raw = json.dumps(record, separators=(",", ":"), allow_nan=False).encode()
        return self.sock.sendto(raw, (host, port))

    def close(self):
        self.stop.set()
        if self.sock is not None:
            self.sock.close()
        if self.thread:
            self.thread.join(timeout=2)

class HostIO:
    def __init__(self, settings, *, routes_module=None, neighbors=None, clock_id=None):
        self.settings = dict(settings)
        self.host_id = self.settings["host_id"]
        if not isinstance(self.host_id, str) or not self.host_id:
            raise ValueError("registered host identity is required")
        self.clock_id = clock_id or boot_clock_id(self.host_id)
        self.routes_module = routes_module or load_routes_module(
            self.settings["device_routes_module_directory"])
        # Use the established constructor and registry, including identity conflict
        # checks and revision semantics. Never create a second route store.
        route_path = Path(self.settings["device_routes"])
        if not route_path.is_file():
            raise ValueError("existing device route registry is required")
        self.routes = self.routes_module.DeviceRoutes(route_path)
        self.neighbors = neighbors or native_neighbors
        self.route_lock = threading.Lock()
        self.stop = threading.Event()
        self.refresh_thread = None
        self._gateway = None
        self._gitlab = None
        self._gateway_lock = threading.Lock()
        self.route_health = dict(state="disabled")
        self.anchors = AnchorHub(self.settings.get("anchors", {}), self.clock_id)

    def clock(self):
        return dict(host_id=self.host_id, clock_id=self.clock_id,
                    monotonic=time.monotonic(), wall_time=time.time())

    def start(self):
        interval = self.settings.get("route_refresh_interval_s", 0)
        if interval:
            if type(interval) not in (int, float) or interval < 1:
                raise ValueError("route refresh interval must be at least one second")
            def run():
                while not self.stop.is_set():
                    try:
                        self.refresh()
                    except Exception as error:
                        self.route_health = dict(state="error", error=type(error).__name__)
                    self.stop.wait(interval)
            self.refresh_thread = threading.Thread(target=run, name="host-device-routes", daemon=True)
            self.refresh_thread.start()
        if self.settings.get("anchors", {}).get("enabled") is True:
            if self.settings.get("enable_effects") is not True:
                raise ValueError("anchor ownership requires explicit effect activation")
            self.anchors.start()

    def close(self):
        self.stop.set()
        self.anchors.close()
        if self.refresh_thread:
            self.refresh_thread.join(timeout=4)

    def refresh(self):
        with self.route_lock:
            changed = self.routes_module.refresh_neighbors(self.routes, self.neighbors())
            provisioning = self.settings.get("device_provisioning", {})
            results = self.routes_module.provision_routes(self.routes, provisioning) if provisioning else []
            self.route_health = dict(state="active", checked_at=time.time(),
                                     changed=len(changed), provisioning=results)
            return self.route_health

    def candidates(self, bssid):
        bssid = str(bssid).lower()
        if not MAC.fullmatch(bssid):
            raise ValueError("invalid anchor BSSID")
        row = self.routes.get("ftm:" + bssid)
        # Preserve the native gateway's station/AP MAC variants.
        parts = [int(part, 16) for part in bssid.split(":")]
        preceding = parts.copy()
        preceding[-1] = (preceding[-1] - 1) % 256
        universal = parts.copy()
        universal[0] &= ~2
        variants = {":".join("%02x" % part for part in row)
                    for row in (parts, preceding, universal)}
        found = [row["host"]] if row else []
        for host, mac in self.neighbors():
            normalized = ":".join(part.zfill(2) for part in mac.split(":"))
            if normalized in variants:
                found.append(host)
        return list(dict.fromkeys(found))

    def observe_anchor(self, bssid, host):
        if self.settings.get("enable_effects") is not True:
            raise PermissionError("host effects are not active")
        bssid = str(bssid).lower()
        if not MAC.fullmatch(bssid):
            raise ValueError("invalid anchor BSSID")
        address = ipaddress.ip_address(host)
        if address.version != 4 or address.is_loopback or address.is_multicast or address.is_unspecified:
            raise ValueError("invalid anchor host")
        return self.routes.observe("ftm:" + bssid, str(address), {"kind": "ftm_anchor", "bssid": bssid})

    def _device_configuration(self, device_id):
        directory = Path(self.settings["vision_directory"])
        front = json.loads((directory / "config.json").read_text()).get("camera", {})
        if front.get("device_id") == device_id:
            return front
        rig = json.loads((directory / "camera-rig.json").read_text())
        rear = rig.get("rear", {}).get("camera", {})
        if rear.get("device_id") == device_id:
            return rear
        raise ValueError("device is not in the configured camera rig")

    def device_request(self, device_id, path, payload=None):
        if path not in DEVICE_PATHS:
            raise ValueError("unsupported native device operation")
        if payload is not None and self.settings.get("enable_effects") is not True:
            raise PermissionError("host effects are not active")
        row = self.routes.get(device_id)
        if row is None:
            raise ValueError("registered device route unavailable")
        cfg = self._device_configuration(device_id)
        host = str(ipaddress.ip_address(row["host"]))
        if ":" in host:
            host = "[" + host + "]"
        parsed = urlsplit(cfg.get("url", ""))
        user = cfg.get("username", unquote(parsed.username or ""))
        password = cfg.get("password", unquote(parsed.password or ""))
        headers = {"Content-Type": "application/json"}
        if user or password:
            headers["Authorization"] = "Basic " + base64.b64encode(
                (user + ":" + password).encode()).decode()
        raw = None if payload is None else json.dumps(payload, allow_nan=False).encode()
        request = Request("http://" + host + path, raw, headers,
                          method="GET" if payload is None else "POST")
        with urlopen(request, timeout=3) as response:
            body = response.read(MAX_JSON + 1)
        if len(body) > MAX_JSON:
            raise ValueError("native device response exceeds limit")
        return json.loads(body)

    def survey_zero(self, world_id, request_id):
        if self.settings.get("enable_effects") is not True:
            raise PermissionError("host effects are not active")
        uuid.UUID(request_id)
        if not isinstance(world_id, str) or not 0 < len(world_id) <= 256:
            raise ValueError("invalid survey world")
        body = dict(type="request", world_id=world_id, request_id=request_id)
        target = self.settings.get("survey_control", {})
        if target.get("url"):
            # The binding is resolved by the runner; the caller never chooses a URL.
            from .host_io_client import HostIOClient
            client = HostIOClient(target)
            with client._request("/v1/survey-zero", body) as response:
                raw = response.read(16385)
        else:
            path = target.get("socket") or str(Path(
                self.settings["vision_directory"]) / "survey-pose.sock")
            with socket.socket(socket.AF_UNIX) as sock:
                sock.settimeout(3)
                sock.connect(path)
                sock.sendall(json.dumps(body).encode() + b"\n")
                with sock.makefile("rb") as source:
                    raw = source.readline(16385)
        if not raw or len(raw) > 16384:
            raise ValueError("invalid survey control response")
        return json.loads(raw)

    def gateway_invoke(self, operation, arguments, request_id=None, realm="site", provider_context=None):
        """Use the native OAuth owner; preserve the caller's operation identity."""
        config = self.settings.get("gateway", {})
        module_path = config.get("module")
        if config.get("enabled") is not True:
            raise PermissionError("native gateway forwarding is not active")
        if (not isinstance(operation, str) or not operation
                or not isinstance(arguments, dict) or realm not in ("site", "local")
                or (request_id is not None and not isinstance(request_id, str))):
            raise ValueError("invalid gateway request")
        if provider_context is not None:
            if (not isinstance(provider_context, dict)
                    or set(provider_context) - {"provider", "host", "addresses"}
                    or not isinstance(provider_context.get("provider"), str)
                    or not isinstance(provider_context.get("host"), str)):
                raise ValueError("invalid provider context")
            if "addresses" in provider_context and (
                    not isinstance(provider_context["addresses"], list)
                    or any(not isinstance(value, str) for value in provider_context["addresses"])):
                raise ValueError("invalid provider addresses")
        path = Path(module_path) if isinstance(module_path, str) else None
        if path is None or not path.is_absolute() or not path.is_file():
            raise ValueError("configured native gateway module is unavailable")
        with self._gateway_lock:
            if self._gateway is None:
                import sys
                if str(path.parent) not in sys.path:
                    sys.path.insert(0, str(path.parent))
                spec = importlib.util.spec_from_file_location("biomerte_host_gateway", path)
                gateway = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(gateway)
                self._gateway = gateway
        return self._gateway.invoke(operation, arguments, request_id, realm,
                                    provider_context=provider_context)

    def gitlab_token(self):
        """Refresh through the native GitLab credential owner, never a VM copy."""
        config = self.settings.get("gitlab", {})
        module_path = config.get("module")
        if config.get("enabled") is not True:
            raise PermissionError("native GitLab credential forwarding is not active")
        path = Path(module_path) if isinstance(module_path, str) else None
        if path is None or not path.is_absolute() or not path.is_file():
            raise ValueError("configured native GitLab module is unavailable")
        with self._gateway_lock:
            if self._gitlab is None:
                import sys
                if str(path.parent) not in sys.path:
                    sys.path.insert(0, str(path.parent))
                spec = importlib.util.spec_from_file_location("biomerte_host_gitlab", path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                self._gitlab = module
        return self._gitlab.token()

    def vision_query(self, query, inputs):
        """Call only the configured existing vision contract on its native socket."""
        policy = self.settings.get("vision_queries", {})
        allowed = policy.get("operations", [])
        mutations = policy.get("mutations", [])
        if (not isinstance(query, str) or query not in allowed
                or not isinstance(inputs, dict)):
            raise ValueError("vision query is not in the configured contract set")
        if query in mutations and self.settings.get("enable_effects") is not True:
            raise PermissionError("host effects are not active")
        if query in ("robot.video.import.read", "robot.video.import.write"):
            # Video processing keeps its native compute dependencies and existing
            # job ledger. The provider has always dispatched these operations
            # through VideoJobs rather than the vision Unix query handler.
            directory = str(Path(self.settings["device_routes_module_directory"]).resolve())
            request = inputs.get("request")
            actions = ({"list", "status"} if query.endswith(".read") else
                       {"create", "chunk", "finish", "start", "cancel"})
            if not isinstance(request, dict) or request.get("action") not in actions:
                raise ValueError("invalid video job action")
            interpreter = Path(policy.get("python", ""))
            if not interpreter.is_absolute() or not interpreter.is_file():
                raise ValueError("native vision interpreter must be explicitly configured")
            # VideoJobs.start launches video_worker with sys.executable. Invoke
            # it in the actual vision environment, not this small bridge's Python.
            program = (
                "import json,sys; sys.path.insert(0,sys.argv[1]); "
                "from video_jobs import VideoJobs; request=json.load(sys.stdin)\n"
                "try: result=VideoJobs(sys.argv[2]).handle(request)\n"
                "except (ValueError,KeyError,FileNotFoundError) as error:\n"
                " result={'ok':False,'error':{'code':'VIDEO_INPUT','message':str(error)[:500]}}\n"
                "json.dump(result,sys.stdout,allow_nan=False)\n")
            completed = subprocess.run(
                [str(interpreter), "-c", program, directory, self.settings["vision_directory"]],
                input=json.dumps(request, allow_nan=False), text=True, capture_output=True)
            if completed.returncode:
                raise RuntimeError("native video request did not complete")
            body = json.loads(completed.stdout)
            if not isinstance(body, dict):
                raise ValueError("native video response must be an object")
            return {"status": 200, "body": body}
        raw = json.dumps({"operation": query, "inputs": inputs},
                         allow_nan=False, separators=(",", ":")).encode()
        if len(raw) > 64 * 1024:
            raise ValueError("native vision request exceeds its contract limit")
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(policy.get("timeout_s"))
            sock.connect(str(Path(self.settings["vision_directory"]) / "vision.sock"))
            connection = http.client.HTTPConnection("localhost")
            connection.sock = sock
            try:
                connection.request("POST", "/v1/query", raw,
                                   {"Content-Type": "application/json"})
                with connection.getresponse() as response:
                    body = response.read(MAX_FRAME + 1)
                    if len(body) > MAX_FRAME:
                        raise ValueError("native vision response exceeds its contract limit")
                    value = json.loads(body)
                    if not isinstance(value, dict):
                        raise ValueError("native vision response must be an object")
                    return {"status": response.status, "body": value}
            finally:
                connection.close()

    def health(self):
        vision = Path(self.settings["vision_directory"]) / "vision.sock"
        provisioning = bool(self.settings.get("device_provisioning") and
                            self.settings.get("route_refresh_interval_s", 0) >= 1)
        device_effects = self.settings.get("enable_effects") is True
        return dict(host_id=self.host_id, clock_id=self.clock_id,
                    routes_available=True, route_refresh=self.route_health,
                    vision_available=vision.exists(), anchors_active=self.anchors.sock is not None,
                    provisioning_active=provisioning, device_commands_active=device_effects,
                    effects_active=provisioning or device_effects or self.anchors.sock is not None)

    def call(self, operation, arguments):
        methods = {"clock": self.clock, "health": self.health,
                   "routes.list": self.routes.list, "routes.get": self.routes.get,
                   "routes.candidates": self.candidates, "routes.observe_anchor": self.observe_anchor,
                   "routes.refresh": self.refresh, "survey.zero": self.survey_zero,
                   "device.request": self.device_request, "anchors.send": self.anchors.send,
                   "vision.query": self.vision_query, "gateway.invoke": self.gateway_invoke,
                   "gitlab.token": self.gitlab_token}
        if operation not in methods:
            raise ValueError("unsupported host operation")
        if operation == "routes.refresh" and self.settings.get("route_refresh_interval_s", 0) < 1:
            raise PermissionError("route refresh is disabled")
        return methods[operation](**arguments)

    @contextmanager
    def vision(self, stream):
        paths = {"live": "/v1/live", "rear": "/v1/live/rear", "map": "/v1/live/map"}
        if stream not in paths:
            raise ValueError("unsupported vision stream")
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(5)
            sock.connect(str(Path(self.settings["vision_directory"]) / "vision.sock"))
            connection = http.client.HTTPConnection("localhost")
            connection.sock = sock
            connection.request("GET", paths[stream])
            with connection.getresponse() as response:
                if response.status != 200:
                    raise OSError("native vision stream is unavailable")
                yield response

def make_server(settings, backend):
    token_file = Path(settings["token_file"])
    if not token_file.is_absolute() or not token_file.is_file():
        raise ValueError("private absolute host token file is required")
    if token_file.stat().st_mode & 0o077:
        raise ValueError("host token file must be owner-only")
    token = token_file.read_text().strip()
    if len(token) < 32:
        raise ValueError("host token must contain at least 32 characters")
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *args):
            pass
        def auth(self):
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied, "Bearer " + token):
                self.reply(401, {"error": "authentication_required"})
                return False
            return True
        def reply(self, status, value):
            raw = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
        def do_POST(self):
            if not self.auth():
                return
            if self.path != "/v1/host-io/call":
                return self.reply(404, {"error": "not_found"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size <= 0 or (size > MAX_JSON and settings.get("gateway", {}).get("enabled") is not True):
                    raise ValueError("invalid request length")
                self.connection.settimeout(5)
                body = json.loads(read_exact(self.rfile, size))
                if body.get("protocol") != PROTOCOL or not isinstance(body.get("arguments"), dict):
                    raise ValueError("invalid protocol")
                result = backend.call(body["operation"], body["arguments"])
                self.reply(200, dict(protocol=PROTOCOL, host_id=backend.host_id,
                                     clock_id=backend.clock_id, result=result))
            except PermissionError:
                self.reply(403, {"error": "operation_not_active"})
            except (ValueError, TypeError, KeyError):
                self.reply(400, {"error": "invalid_host_request"})
            except (OSError, RuntimeError):
                self.reply(503, {"error": "native_dependency_unavailable"})
        def do_GET(self):
            if not self.auth():
                return
            self.connection.settimeout(5)
            try:
                if self.path.startswith("/v1/host-io/vision/"):
                    stream = self.path.rsplit("/", 1)[-1]
                    with backend.vision(stream) as source:
                        self.send_response(200)
                        self.send_header("Content-Type", "application/octet-stream")
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.close_connection = True
                        while not backend.stop.is_set():
                            total, size = struct.unpack("!II", read_exact(source, 8))
                            if not 4 <= total <= MAX_FRAME or not 0 < size <= min(MAX_JSON, total - 4):
                                raise ValueError("invalid native vision frame")
                            metadata = json.loads(read_exact(source, size))
                            jpeg = read_exact(source, total - 4 - size)
                            envelope = json.dumps(dict(metadata=metadata, clock=backend.clock()),
                                                  allow_nan=False, separators=(",", ":")).encode()
                            if len(envelope) > MAX_JSON:
                                raise ValueError("native metadata exceeds limit")
                            self.wfile.write(struct.pack("!II", len(envelope) + len(jpeg) + 4,
                                                         len(envelope)) + envelope + jpeg)
                elif self.path == "/v1/host-io/anchors":
                    with backend.anchors.subscribe() as inbox:
                        self.send_response(200)
                        self.send_header("Content-Type", "application/x-ndjson")
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.close_connection = True
                        while not backend.stop.is_set():
                            try:
                                item = inbox.get(timeout=1)
                            except queue.Empty:
                                item = dict(heartbeat=True, clock_id=backend.clock_id)
                            self.wfile.write(json.dumps(item, separators=(",", ":")).encode() + b"\n")
                else:
                    self.reply(404, {"error": "not_found"})
            except (OSError, ValueError, EOFError, struct.error):
                self.close_connection = True
    listen = settings.get("listen", "127.0.0.1")
    address = ipaddress.ip_address(listen)
    tls = settings.get("tls")
    if not address.is_loopback and not tls and not (
            settings.get("allow_local_http") is True and (address.is_private or address.is_unspecified)):
        raise ValueError("host I/O bind requires TLS or explicit private local HTTP")
    server = ThreadingHTTPServer((listen, int(settings.get("port", 8765))), Handler)
    server.daemon_threads = True
    if tls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(tls["cert_file"], tls["key_file"])
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    settings = json.loads(Path(args.config).read_text())
    backend = HostIO(settings)
    server = make_server(settings, backend)
    try:
        backend.start()
        server.serve_forever(poll_interval=.5)
    finally:
        backend.close()
        server.server_close()

if __name__ == "__main__":
    main()
