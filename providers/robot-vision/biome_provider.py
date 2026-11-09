#!/usr/bin/env python3
"""Short, durable BiomeRTE queries for the independent robot vision service."""
from __future__ import annotations

import argparse
import base64
import binascii
import copy
import fcntl
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import stat
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
DEFAULT_STATE = Path.home() / ".local/share/biome-robot-vision"
CHUNK_BYTES = 48 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def field(kind, required=False):
    """cap.define accepts shorthand types; discovery returns normalized fields."""
    return kind + ("" if required else "?")


def contract_definitions():
    """Versioned envelopes; telemetry itself can evolve without false precision."""
    specs = [
        ("robot.vision.snapshot", {"snapshot_id": field("str"), "include_frame": field("bool")},
         "One coherent retained frame, pose and map revision; image bytes are opt-in."),
        ("robot.vision.frame", {"snapshot_id": field("str"), "frame_id": field("str")},
         "An exact retained frame as verified artifact chunks, with capture metadata."),
        ("robot.vision.detections", {"snapshot_id": field("str")},
         "Current-frame detections and tracker-epoch identities with quality and freshness."),
        ("robot.vision.geometry", {"snapshot_id": field("str")},
         "Localization and spatial evidence with explicit availability, quality and unknown coverage."),
        ("robot.vision.metadata", {}, "Source, calibration, implementation and coordinate metadata."),
        ("robot.vision.status", {}, "Camera, transport, decoding, inference and provider health."),
        ("robot.map.read", {"map_id": field("str"), "revision": field("int"),
                           "bounds": field("obj"), "kinds": field("list"),
                           "cursor": field("str"), "limit": field("int")},
         "A bounded map page; bounds uses min_x,min_y,max_x,max_y and optional min_z,max_z in map units."),
        ("robot.map.changes", {"map_id": field("str"), "since_revision": field("int", True),
                              "to_revision": field("int"), "cursor": field("str"),
                              "limit": field("int")},
         "Persistent changes between explicit map revisions."),
        ("robot.map.checkpoint", {"map_id": field("str")},
         "Flush the persistent map and return its durable checkpoint identity."),
        ("robot.map.create", {"metadata": field("obj")},
         "Create a new map identity; this does not establish localization or metric scale."),
        ("robot.map.open", {"map_id": field("str", True)},
         "Select an existing persistent map; localization must be re-established."),
        ("robot.vision.calibration", {"action": field("str"), "calibration": field("obj"), "calibration_id": field("str")},
         "Get or set calibration. action is get or set; unverified geometry remains explicit."),
        ("robot.vision.project_ground", {"pixel": field("list", True),
                                        "confirmed_floor_contact": field("bool", True),
                                        "snapshot_id": field("str")},
         "Intersect an individually confirmed floor pixel with the calibrated ground plane."),
    ]
    return [dict(name=name, version=1, description=description, inputs=inputs,
                 outputs={"result": "obj"}) for name, inputs, description in specs]


CONTRACTS = {item["name"] + ":1": item for item in contract_definitions()}


class ServiceError(RuntimeError):
    def __init__(self, code, message):
        self.code = str(code)[:80]
        self.message = str(message)[:240]
        super().__init__(self.message)


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        try:
            self.sock.connect(self.path)
        except BaseException:
            self.sock.close()
            self.sock = None
            raise


def check_socket(path):
    path = Path(path)
    parent = path.parent.stat()
    info = path.lstat()
    if (parent.st_uid != os.getuid() or parent.st_mode & 0o077 or
            info.st_uid != os.getuid() or info.st_mode & 0o077 or
            not stat.S_ISSOCK(info.st_mode)):
        raise ServiceError("unsafe_socket", "Vision socket and its directory must be owner-only.")


class VisionClient:
    def __init__(self, path, timeout=None):
        self.path, self.timeout = Path(path), timeout

    def query(self, operation, inputs):
        if operation + ":1" not in CONTRACTS or not isinstance(inputs, dict):
            raise ServiceError("invalid_request", "Unknown operation or invalid inputs.")
        try:
            check_socket(self.path)
            body = json.dumps({"operation": operation, "inputs": inputs},
                              separators=(",", ":"), allow_nan=False).encode()
        except (FileNotFoundError, ConnectionError, OSError) as exc:
            raise ServiceError("service_unavailable", "Local vision service is unavailable.") from exc
        if len(body) > 64 * 1024:
            raise ServiceError("request_too_large", "Vision request exceeds 64 KiB.")
        connection = UnixHTTPConnection(self.path, self.timeout)
        try:
            connection.request("POST", "/v1/query", body=body,
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ServiceError("response_too_large", "Vision response exceeds the transport limit.")
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeDecodeError) as exc:
                raise ServiceError("invalid_response", "Local service returned invalid JSON.") from exc
            if not isinstance(result, dict):
                raise ServiceError("invalid_response", "Local service response must be an object.")
            if not 200 <= response.status < 300:
                error = result.get("error", {})
                if not isinstance(error, dict):
                    error = {}
                raise ServiceError(error.get("code", "service_error"),
                                   error.get("message", "Local vision query failed."))
            return result
        except (OSError, http.client.HTTPException) as exc:
            raise ServiceError("service_unavailable", "Local vision query did not complete.") from exc
        finally:
            connection.close()


def store_bytes(data, remote):
    chunks = []
    for start in range(0, len(data), CHUNK_BYTES):
        chunk = data[start:start + CHUNK_BYTES]
        digest = hashlib.sha256(chunk).hexdigest()
        saved = remote.call("artifact.put", {"data": base64.b64encode(chunk).decode("ascii")})
        if not isinstance(saved, dict) or saved.get("sha256") != digest:
            raise ServiceError("artifact_integrity", "Artifact chunk checksum verification failed.")
        chunks.append({"sha256": digest, "size": len(chunk)})
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "chunks": chunks}


def externalize_frame(payload, remote, requested):
    """Only a caller-requested frame crosses the gateway, without inline bytes."""
    result = copy.deepcopy(payload)
    frame = result.get("frame")
    if isinstance(frame, dict) and "data_base64" in frame:
        encoded = frame.pop("data_base64")
        if requested:
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError, binascii.Error) as exc:
                raise ServiceError("invalid_frame", "Local frame encoding is invalid.") from exc
            if not data:
                raise ServiceError("invalid_frame", "Local frame is empty.")
            digest = hashlib.sha256(data).hexdigest()
            if frame.get("sha256") not in (None, digest):
                raise ServiceError("frame_integrity", "Local frame checksum does not match.")
            if frame.get("byte_count") not in (None, len(data)):
                raise ServiceError("frame_integrity", "Local frame byte count does not match.")
            frame["artifact"] = store_bytes(data, remote)
            frame["sha256"], frame["byte_count"] = digest, len(data)

    def remove_inline(value):
        if isinstance(value, dict):
            value.pop("data_base64", None)
            for child in value.values():
                remove_inline(child)
        elif isinstance(value, list):
            for child in value:
                remove_inline(child)
    remove_inline(result)
    return result


def execute(action, cfg, remote, client=None):
    contract = action.get("contract")
    if contract not in CONTRACTS:
        raise ServiceError("unsupported_contract", "This provider does not implement the contract.")
    inputs = action.get("inputs", {})
    if not isinstance(inputs, dict):
        raise ServiceError("invalid_request", "Inputs must be an object.")
    operation = CONTRACTS[contract]["name"]
    requested = operation == "robot.vision.frame" or (
        operation == "robot.vision.snapshot" and inputs.get("include_frame") is True)
    client = client or VisionClient(cfg["socket"], cfg.get("query_timeout_seconds"))
    response = client.query(operation, inputs)
    return {"result": externalize_frame(response, remote, requested)}


def load_gateway(directory):
    directory = Path(directory).expanduser().resolve()
    path = directory / "mac_provider.py"
    if not path.is_file():
        raise RuntimeError("Installed mac-terminal gateway transport is unavailable.")
    sys.path.insert(0, str(directory))
    spec = importlib.util.spec_from_file_location("robot_vision_gateway", path)
    gateway = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gateway)
    return gateway


class Remote:
    realm = "site"

    def __init__(self, cfg, gateway=None):
        self.node, self.site = cfg["provider"], cfg["site"]
        self.gateway = gateway or load_gateway(cfg["gateway_state"])

    def call(self, name, args, rid=None):
        return self.gateway.invoke(name, args, rid, self.realm)


def offer(cfg, contract, state):
    return dict(provider=cfg["provider"], contract=contract, resource=cfg["resource"],
                service="robot-vision", capacity=cfg["slots"], state=state,
                usage={
                    "before": "Acquire this contract/resource or use cap.submit. Read snapshot IDs, "
                              "map revisions, freshness and quality before interpreting telemetry.",
                    "read_result": "Read cap.status.result.result for the telemetry object; its error "
                                   "field reports a completed query failure. Requested frames use "
                                   "artifact.get for each SHA256 chunk in frame.artifact; verify the "
                                   "complete SHA256 after concatenation."
                })


def supported_contracts(status):
    capabilities = status.get("capabilities", [])
    if not isinstance(capabilities, list):
        return set()
    names = {name for name in capabilities if isinstance(name, str)}
    return {contract for contract, spec in CONTRACTS.items()
            if contract in names or spec["name"] in names}


def read_registered(path, cfg):
    path = Path(path)
    if not path.exists():
        return set()
    record = json.loads(path.read_text())
    if record.get("authority") != [cfg["site"], "site", cfg["provider"]]:
        raise RuntimeError("Advertisement record belongs to another provider authority.")
    return {contract for contract in record.get("contracts", []) if contract in CONTRACTS}


def write_registered(path, cfg, contracts):
    """Remember prior offers so a service outage after restart marks them offline."""
    path = Path(path)
    data = json.dumps({"authority": [cfg["site"], "site", cfg["provider"]],
                       "contracts": sorted(contracts)}).encode()
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_config(state):
    state = Path(state).expanduser().resolve()
    cfg = json.loads((state / "provider-manifest.json").read_text())
    if Path(cfg["state"]).resolve() != state:
        raise RuntimeError("Provider manifest belongs to another state directory.")
    return cfg


def worker_offers(cfg):
    base = [cfg["python"], str(Path(cfg["repo"]) / "providers/robot-vision/biome_provider.py"),
            "execute", "--state", cfg["state"]]
    return [dict(offer(cfg, contract, "ready"), argv=base + ["--contract", contract])
            for contract in CONTRACTS]


def serve(cfg):
    os.umask(0o077)
    sys.path.insert(0, cfg["repo"])
    from biomerte.worker import ActionPool, Worker

    worker_state = Path(cfg["state"]) / "provider"
    worker_state.mkdir(mode=0o700, parents=True, exist_ok=True)
    worker_state.chmod(0o700)
    remote = Remote(cfg)
    client = VisionClient(cfg["socket"], cfg.get("health_timeout_seconds", 5))
    local = worker_offers(cfg)
    with open(worker_state / "worker.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        worker = Worker(str(worker_state / "worker.sqlite"), remote, local,
                        cfg.get("worker_timeout_seconds"))
        pool = ActionPool(worker, cfg["slots"])
        registry = worker_state / "registered-contracts.json"
        advertised, registered = {}, set()
        defined = read_registered(registry, cfg)
        checked, stopping = 0.0, False

        def stop(_sig, _frame):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            while not stopping:
                try:
                    now = time.monotonic()
                    if now >= checked:
                        try:
                            ready = supported_contracts(client.query("robot.vision.status", {}))
                        except ServiceError:
                            ready = set()
                        for contract in sorted(ready - registered):
                            remote.call("cap.define", CONTRACTS[contract])
                            registered.add(contract)
                            defined.add(contract)
                        write_registered(registry, cfg, defined)
                        for contract in sorted(defined):
                            state = "ready" if contract in ready else "offline"
                            if advertised.get(contract) != state:
                                remote.call("cap.offer", offer(cfg, contract, state))
                                advertised[contract] = state
                        checked = now + cfg.get("health_seconds", 10)
                    if defined:
                        worker.once(pool)
                    time.sleep(cfg.get("poll_seconds", 2))
                except Exception as exc:
                    # Request/image bytes and gateway credential errors never enter logs.
                    print("Vision provider retry:", type(exc).__name__, file=sys.stderr, flush=True)
                    time.sleep(5)
        finally:
            try:
                for contract in sorted(defined):
                    try:
                        remote.call("cap.offer", offer(cfg, contract, "offline"))
                    except Exception:
                        pass
            finally:
                pool.executor.shutdown(wait=True)
                worker.db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("serve", "execute"))
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--contract", choices=tuple(CONTRACTS))
    args = parser.parse_args(argv)
    if args.mode == "execute" and args.contract is None:
        parser.error("execute requires --contract")
    cfg = load_config(args.state)
    if args.mode == "serve":
        serve(cfg)
        return 0
    try:
        action = json.load(sys.stdin)
        action["contract"] = args.contract
        output = execute(action, cfg, Remote(cfg))
    except ServiceError as exc:
        error = {"code": exc.code, "message": exc.message}
        if exc.code == "service_unavailable" and args.contract in {
                "robot.map.create:1", "robot.map.open:1", "robot.map.checkpoint:1",
                "robot.vision.calibration:1"}:
            error.update(effect="unknown", next_action="Inspect current state before a new mutation request.")
        # A handled query error is a completed transport operation. The shared Worker
        # reserves nonzero exits for genuinely uncertain adapter execution.
        json.dump({"result": {"error": error}}, sys.stdout)
        return 0
    json.dump(output, sys.stdout, allow_nan=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
