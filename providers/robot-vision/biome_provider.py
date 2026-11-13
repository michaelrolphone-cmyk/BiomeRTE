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
import threading
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
        ("robot.camera.exposure.status", {}, "Read live camera auto-exposure, auto-gain and manual driver control values; first-DMA capture stamps are not calibrated photon exposure times."),
        ("robot.camera.exposure.set", {"auto_exposure":field("bool"),"auto_exposure_dsp":field("bool"),"auto_gain":field("bool"),"exposure_value":field("int"),"exposure_level":field("int"),"gain":field("int")}, "Partial camera exposure/gain update. Auto modes are Boolean; exposure_value 9..1200 sensor-driver units (not microseconds); exposure_level -2..2; gain 0..30. Settings are RAM-only. Inspect unknown outcomes before retry."),
        ("robot.body.model", {}, "Metric body dimensions, wheelbase, track, camera/sonar/AHRS rigid mounts and always-on-floor invariant."),
        ("robot.control.model", {}, "Relay motion map: 1 forward, 2 back, 3 left, 4 right. Binary on/off, no PWM; speed depends on continuous on-duration; curve not yet calibrated."),
        ("robot.sonar.snapshot", {}, "Latest four fused body-mounted sonar ranges in meters with per-reading age, validity, freshness, sensor poses and polling update rate."),
        ("robot.metric.status", {}, "Available metric geometry, sonar units and separately scoped native-map scale attachment and provenance."),
        ("robot.vision.snapshot", {"snapshot_id": field("str"), "include_frame": field("bool")},
         "One coherent retained frame, pose and map revision; image bytes are opt-in."),
        ("robot.vision.frame", {"snapshot_id": field("str"), "frame_id": field("str")},
         "An exact retained frame as verified artifact chunks, with capture metadata."),
        ("robot.vision.detections", {"snapshot_id": field("str")},
         "Current-frame detections and tracker-epoch identities with quality and freshness."),
        ("robot.vision.geometry", {"snapshot_id": field("str")},
         "Localization and spatial evidence with explicit availability, quality and unknown coverage."),
        ("robot.vision.metadata", {}, "Camera and body metadata; robot.camera.light.set:1 illuminates the view for driving. Capture timestamps are first-DMA events, not calibrated exposure start/midpoint/end."),
        ("robot.vision.status", {}, "Camera, transport, decoding, inference and provider health."),
        ("robot.tof.status", {}, "FTM anchor health and fresh ranging evidence; qualification and missing geometry remain explicit."),
        ("robot.tof.localize", {}, "Current ToF position solution or explicit unavailable status; no unqualified navigation authority."),
        ("robot.camera.light.status", {},
         "Read authenticated camera light command/driver state; physical readback remains as reported."),
        ("robot.camera.light.set", {"on": field("bool"), "brightness": field("int"), "rgb": field("list")},
         "Illuminate the camera view for driving using the onboard LED. One-shot partial camera light update: on, brightness 0..255, rgb three bytes; inspect unknown outcomes before retry."),
        ("robot.ahrs.status", {},
         "Cached AHRS health, per-sensor freshness, attitude reference and calibration command state."),
        ("robot.ahrs.snapshot", {},
         "Cached full camera AHRS telemetry with sensor frames, units, ages and host timing qualification."),
        ("robot.ahrs.calibrate", {},
         "One-shot gyro calibration request; hold the board still for the firmware measurement window, then inspect status."),
        ("robot.map.read", {"map_id": field("str"), "revision": field("int"),
                           "bounds": field("obj"), "kinds": field("list"),
                           "cursor": field("str"), "limit": field("int")},
         "A bounded map page; bounds uses min_x,min_y,max_x,max_y and optional min_z,max_z in map units."),
        ("robot.world.read", {"cursor": field("str"), "limit": field("int")},
         "Persistent world geometry; unaligned local submaps remain explicitly separate."),
        ("robot.world.status", {}, "Persistent world identity, submap transforms and alignment evidence."),
        ("robot.map.obstacles", {"map_id": field("str"), "revision": field("int")},
         "Accumulated mounted sonar rays, obstacle voxels, clearing evidence and uncertainty at a map revision."),
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
                                        "snapshot_id": field("str")},
         "Intersect a pixel ray with the calibrated floor plane; rover body is always on the floor, no caller contact assertion. Ray intersection does not classify object contact."),

        ("robot.map.scale.start", {"method": field("str", True),
                                   "reference": field("obj"), "policy": field("obj"),
                                   "auto_capture": field("bool"), "auto_apply": field("bool")},
         "Start a persistent checkerboard or measured_distance scale session; never drives the vehicle."),
        ("robot.map.scale.status", {"session_id": field("str"), "map_id": field("str"),
                                    "include_observations": field("bool")},
         "Read scale-session evidence, fit state and exact-keyframe capture eligibility."),
        ("robot.map.scale.capture", {"session_id": field("str", True),
                                     "snapshot_id": field("str", True),
                                     "role": field("str"), "label": field("str")},
         "Retain one exact native-keyframe snapshot as scale evidence; no interpolated poses."),
        ("robot.map.scale.fit", {"session_id": field("str", True),
                                 "distance_m": field("num"),
                                 "validation_distance_m": field("num")},
         "Fit scale with independent validation; unverified reference dimensions remain provisional."),
        ("robot.map.scale.apply", {"session_id": field("str", True)},
         "Attach a qualified metric scale without changing the map origin or axes."),
        ("robot.map.scale.cancel", {"session_id": field("str", True)},
         "Cancel a scale session and return its persisted state."),
        ("robot.map.scale.reference", {"session_id": field("str", True),
                                       "reference": field("obj", True)},
         "Update an unapplied board reference, recompute preserved board poses and invalidate its prior candidate."),
        ("robot.map.metric.read", {"map_id": field("str"), "revision": field("int"),
                                   "attachment_id": field("str"), "bounds": field("obj"),
                                   "kinds": field("list"), "cursor": field("obj"), "limit": field("int")},
         "Read a revision-pinned metric map view with explicit qualification and stale-state reporting."),
    ]
    definitions = [dict(name=name, version=1, description=description, inputs=inputs,
                        outputs={"result": "obj"}) for name, inputs, description in specs]
    ground = next(item for item in definitions if item["name"] == "robot.vision.project_ground")
    modern = copy.deepcopy(ground)
    modern["version"] = 2
    ground["inputs"]["confirmed_floor_contact"] = field("bool", True)
    ground["description"] = "Legacy floor query; prefer robot.vision.project_ground:2, which uses the body floor-contact invariant without a caller assertion."
    definitions.append(modern)
    legacy_descriptions = {'robot.vision.metadata': 'Source, calibration, implementation and coordinate metadata.', 'robot.camera.light.set': 'One-shot partial camera light update: on, brightness 0..255, rgb three bytes; inspect unknown outcomes before retry.', 'robot.vision.project_ground': 'Intersect an individually confirmed floor pixel with the calibrated ground plane.'}
    legacy_guidance = {'robot.vision.metadata': {'before': 'Read robot.body.model:1 and robot.metric.status:1. robot.camera.light.set:1 illuminates the view for driving. First-DMA capture timestamps are not calibrated exposure times.'}, 'robot.camera.light.set': {'before': 'The onboard LED illuminates the camera view for driving. Partial on/brightness/rgb update; inspect uncertain outcomes before retry.'}, 'robot.vision.project_ground': {'before': 'Legacy v1 schema; use robot.vision.project_ground:2 to project onto the floor plane without a caller contact assertion.'}}
    for item in definitions:
        if item["version"] == 1 and item["name"] in legacy_descriptions:
            item["description"] = legacy_descriptions[item["name"]]
            item["usage"] = legacy_guidance[item["name"]]
    return definitions


CONTRACTS = {item["name"] + ":" + str(item["version"]): item for item in contract_definitions()}


MUTATING_CONTRACTS = frozenset({
    "robot.camera.exposure.set:1",
    "robot.map.create:1", "robot.map.open:1", "robot.map.checkpoint:1",
    "robot.camera.light.set:1", "robot.ahrs.calibrate:1",
    "robot.vision.calibration:1",
    "robot.map.scale.start:1", "robot.map.scale.capture:1", "robot.map.scale.fit:1",
    "robot.map.scale.apply:1", "robot.map.scale.cancel:1", "robot.map.scale.reference:1",
})

SCALE_GUIDANCE = {
    "robot.map.scale.start:1": {
        "before": "method is checkerboard or measured_distance. A checkerboard reference requires "
                  "positive square_size_m, size_verified, measurement_source and stationary=true; "
                  "inner_corners defaults to [9,6]. Unmeasured physical size must remain unverified. "
                  "auto_capture defaults true for checkerboards; auto_apply defaults false. "
                  "Vehicle motion is supplied independently.",
        "read_result": "Keep the returned session identity. States are collecting, ready, provisional, "
                       "applied or canceled. Provisional evidence is not an applied metric scale.",
    },
    "robot.map.scale.status:1": {
        "before": "Select session_id or map_id when inspecting a preserved session. Set "
                  "include_observations=true to inspect the retained measurement evidence.",
        "read_result": "Check fit qualification and the last eligible exact-keyframe snapshot. "
                       "Current checkerboard visibility does not imply capture eligibility.",
    },
    "robot.map.scale.capture:1": {
        "before": "snapshot_id must identify an exact native keyframe from this map/session, with the "
                  "same source_frame_id. role is training or validation. For measured_distance, label "
                  "is start, end, validation_start or validation_end. Measure displacement between "
                  "the actual camera optical-center endpoints represented by those snapshots.",
        "read_result": "Keep the retained evidence identity and role. A rejected or ineligible frame "
                       "must not be treated as an interpolated endpoint.",
    },
    "robot.map.scale.fit:1": {
        "before": "Use separate held-out validation evidence. For measured_distance provide positive "
                  "distance_m and validation_distance_m measured between each pair of camera optical "
                  "centers; neither value is integrated path length or a drive command.",
        "read_result": "A failed fit remains collecting. Unverified board dimensions remain provisional. "
                       "Only a qualified fit may become ready or be auto-applied.",
    },
    "robot.map.scale.apply:1": {
        "before": "Inspect this session's qualified fit first. Application preserves map origin and "
                  "axes and never commands vehicle motion.",
        "read_result": "Retain the metric attachment identity and source revision. Check metric_scale "
                       "availability before interpreting metric_localization or metric map coordinates.",
    },
    "robot.map.scale.cancel:1": {
        "before": "Use the exact session_id whose scale collection should end.",
        "read_result": "Read the returned persisted session state before starting replacement work.",
    },
    "robot.map.scale.reference:1": {
        "before": "Supply the complete corrected checkerboard reference before application. Verified "
                  "square_size_m must come from a physical measurement with measurement_source. "
                  "An already-applied reference requires a new session.",
        "read_result": "Preserved raw corners are reprocessed under the new reference and the prior "
                       "candidate is invalidated. Fit again before applying.",
    },
    "robot.map.metric.read:1": {
        "before": "Bounds are in metres. Pin map_id, revision and attachment_id when comparing pages; "
                  "carry the returned cursor unchanged as a JSON object. The raw map.read contract keeps arbitrary units.",
        "read_result": "Require an available, qualified metric attachment. Stale or unresolved anchor "
                       "corrections are explicit. Scaling preserves origin and axes; sparse geometry "
                       "does not establish floor alignment, gravity, occupancy or free space.",
    },
}

AHRS_GUIDANCE = {
    "robot.ahrs.status:1": {
        "before": "This reads the local cache and never contacts the camera. Background AHRS "
                  "polling is opt-in and independent of radio polling.",
        "read_result": "Read each sensor validity and age, receiver/boot/attitude epoch, faults, "
                       "and gyro_bias_valid. A command receipt and subsequent calibration state "
                       "are separate evidence; firmware provides no calibration request identifier.",
    },
    "robot.ahrs.snapshot:1": {
        "before": "This returns cached full telemetry without a camera request.",
        "read_result": "Acceleration includes gravity; yaw is relative; magnetometer axes are "
                       "distinct and not a calibrated heading. Pressure altitude is not floor "
                       "height. Sensor mounting and camera exposure timing remain unqualified; "
                       "this evidence does not establish a camera/world pose or metric distance.",
    },
    "robot.ahrs.calibrate:1": {
        "before": "Hold the board still, invoke once with empty inputs, and keep it stationary "
                  "through the approximately two-second firmware measurement window. This "
                  "explicit command is available even when background AHRS polling is disabled.",
        "read_result": "Queued means accepted, not completed. Read robot.ahrs.status or "
                       "robot.ahrs.snapshot for later state when polling is enabled, or the "
                       "camera AHRS panel when disabled. Check gyro_bias_valid; calibration is "
                       "RAM-only and success resets attitude. Inspect unknown outcomes before "
                       "any new request; do not automatically retry.",
    },
}


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
        return self.gateway.invoke_provider(name, args, rid, self.realm, self.node)


def offer(cfg, contract, state):
    usage = {
        "before": "Acquire this contract/resource or use cap.submit. Read snapshot IDs, "
                  "map revisions, freshness and quality before interpreting telemetry.",
        "read_result": "Read cap.status.result.result for the telemetry object; its error "
                       "field reports a completed query failure. Requested frames use "
                       "artifact.get for each SHA256 chunk in frame.artifact; verify the "
                       "complete SHA256 after concatenation."
    }
    for name, guidance in {**SCALE_GUIDANCE.get(contract, {}),
                           **AHRS_GUIDANCE.get(contract, {})}.items():
        usage[name] += " " + guidance
    usage.update(CONTRACTS[contract].get("usage", {}))
    return dict(provider=cfg["provider"], contract=contract, resource=cfg["resource"],
                service="robot-vision", capacity=cfg["slots"], state=state, usage=usage)

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
    base = [cfg["python"], str(cfg.get("adapter_path") or (Path(cfg["repo"]) / "providers/robot-vision/biome_provider.py")),
            "execute", "--state", cfg["state"]]
    return [dict(offer(cfg, contract, "ready"), argv=base + ["--contract", contract])
            for contract in CONTRACTS]


def stream_offer(cfg, status):
    """Advertise only a service that actually supplies both native live feeds."""
    live = status.get("streaming", {})
    available = (status.get("state") in ("running", "starting") and
                 live.get("available") is True and
                 set(live.get("lanes", [])) >= {"vision", "map"})
    return dict(provider=cfg["provider"], contract="robot.vision.stream:1",
                resource=cfg["resource"], service="robot-vision", capacity=cfg["slots"],
                state="ready" if available else "offline",
                configuration={"lease_class": "persistent", "stream_only": True,
                    "stream": {"enabled": True, "direction": "provider_to_server",
                        "delivery": "supersedable", "lanes": ["vision", "map"],
                        "policies": {"vision": "supersedable", "map": "ordered"},
                        "buffer_depth": 8, "max_message_bytes": 2097152}})


def serve(cfg):
    os.umask(0o077)
    sys.path.insert(0, cfg["repo"])
    from biomerte.worker import ActionPool, Worker

    worker_state = Path(cfg["state"]) / "provider"
    worker_state.mkdir(mode=0o700, parents=True, exist_ok=True)
    worker_state.chmod(0o700)
    remote = Remote(cfg)
    from leased_streams import VisionStreams
    live_streams = VisionStreams(remote, cfg["socket"], cfg.get("stream_local_router_url"),
                                 sequence_path=worker_state / "stream-sequences.sqlite")
    client = VisionClient(cfg["socket"], cfg.get("health_timeout_seconds", 5))
    local = worker_offers(cfg)
    with open(worker_state / "worker.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        worker = Worker(str(worker_state / "worker.sqlite"), remote, local,
                        cfg.get("worker_timeout_seconds"))
        worker.stream_handler = live_streams.update
        pool = ActionPool(worker, cfg["slots"])
        registry = worker_state / "registered-contracts.json"
        advertised, registered = {}, set()
        defined = read_registered(registry, cfg)
        stopping = threading.Event()

        def stop(_sig, _frame):
            stopping.set()

        def advertise():
            retired_withdrawn=set()
            relay_metadata_published = False
            # Registration and health may need remote round trips. They must
            # not block the worker inbox or an already leased live stream.
            while not stopping.is_set():
                try:
                    try:
                        status = client.query("robot.vision.status", {"provider_only": True})
                        ready = supported_contracts(status)
                    except ServiceError as error:
                        # A busy/late health reply is unknown, not provider loss.
                        # Only a confirmed missing endpoint or unsafe socket
                        # withdraws offers; explicit service status still governs.
                        if (isinstance(error.__cause__, (FileNotFoundError, ConnectionRefusedError))
                                or error.code == "unsafe_socket"):
                            status = {}
                            ready = set()
                        else:
                            raise
                    if not relay_metadata_published:
                        for spec in json.loads((HERE / "rover-relay-contracts.json").read_text()):
                            remote.call("cap.define", spec)
                        relay_metadata_published = True
                    remote.call("cap.define", {"name":"robot.vision.stream","version":1,"inputs":{},"outputs":{},
                        "description":"Continuous processed frames, pose and telemetry under a persistent native lease"})
                    if stopping.is_set():return
                    # Withdraw the retired RSSI/BLE implementations before advertising ToF.
                    for retired in {"robot.radio.status:1","robot.radio.localize:1","robot.map.radio.read:1"}-retired_withdrawn:
                        remote.call("cap.offer",dict(provider=cfg["provider"],contract=retired,
                            resource=cfg["resource"],service="robot-vision",capacity=cfg["slots"],state="offline"))
                        retired_withdrawn.add(retired)
                    remote.call("cap.offer", stream_offer(cfg, status))
                    for contract in sorted(ready - registered):
                        if stopping.is_set():return
                        remote.call("cap.define", CONTRACTS[contract])
                        registered.add(contract)
                        defined.add(contract)
                    write_registered(registry, cfg, defined)
                    for contract in sorted(defined):
                        if stopping.is_set():return
                        state = "ready" if contract in ready else "offline"
                        if advertised.get(contract) != state:
                            remote.call("cap.offer", offer(cfg, contract, state))
                            advertised[contract] = state
                    stopping.wait(cfg.get("health_seconds", 10))
                except Exception as exc:
                    print("Vision advertisement retry:", type(exc).__name__, file=sys.stderr, flush=True)
                    stopping.wait(5)

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        publisher = threading.Thread(target=advertise, name="vision-advertisement", daemon=True)
        publisher.start()
        try:
            while not stopping.is_set():
                try:
                    worker.once(pool)
                    stopping.wait(cfg.get("poll_seconds", 2))
                except Exception as exc:
                    # Request/image bytes and gateway credential errors never enter logs.
                    print("Vision provider retry:", type(exc).__name__, file=sys.stderr, flush=True)
                    stopping.wait(5)
        finally:
            stopping.set()
            publisher.join()
            try:
                try:
                    remote.call("cap.offer", stream_offer(cfg, {}))
                except Exception:
                    pass
                for contract in sorted(defined):
                    try:
                        remote.call("cap.offer", offer(cfg, contract, "offline"))
                    except Exception:
                        pass
            finally:
                live_streams.close()
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
        if (exc.code in ("service_unavailable", "invalid_response", "response_too_large")
                and args.contract in MUTATING_CONTRACTS):
            error.update(effect="unknown", next_action="Inspect current state before a new mutation request.")
        # A handled query error is a completed transport operation. The shared Worker
        # reserves nonzero exits for genuinely uncertain adapter execution.
        json.dump({"result": {"error": error}}, sys.stdout)
        return 0
    json.dump(output, sys.stdout, allow_nan=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
