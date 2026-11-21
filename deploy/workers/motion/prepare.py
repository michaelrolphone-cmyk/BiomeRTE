"""Prepare a concrete motion handoff bundle without starting or stopping anything.

Run on the native Mac while its motion database still has native ownership.
The bundle keeps database/WAL files in place and preserves the worker identity.
"""
import argparse
import datetime
from contextlib import contextmanager
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import sqlite3
import subprocess
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
IMAGE = "sha256:3bb07e94f55d8ddaf1ff2457d727df79050618b1e9e3d6ebc09761324acd335c"
LABELS = ("org.biomerte.rover-motion", "org.biomerte.rover-motion-provider")


def read_object(path):
    result = json.loads(Path(path).read_text())
    if not isinstance(result, dict):
        raise ValueError(str(path) + " must contain an object")
    return result


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_private(path, value):
    data = value if isinstance(value, bytes) else value.encode()
    with Path(path).open("xb") as stream:
        stream.write(data)
    Path(path).chmod(0o600)


def write_json(path, value):
    write_private(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


@contextmanager
def readonly_database(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        yield db
    finally:
        db.close()


def custody_snapshot(native):
    worker = Path(native["state"]) / "worker.sqlite"
    observations = Path(native["database"])
    with readonly_database(worker) as db:
        worker_summary = {
            "actions_by_state": dict(db.execute(
                "SELECT state,COUNT(*) FROM actions GROUP BY state").fetchall()),
            "recovery_results": [
                {"id": row[0], "state": row[1], "has_result": bool(row[2]),
                 "acknowledged": bool(row[3])}
                for row in db.execute(
                    "SELECT p.id,a.state,a.result IS NOT NULL,k.id IS NOT NULL "
                    "FROM result_recovery_pending p LEFT JOIN actions a ON a.id=p.id "
                    "LEFT JOIN result_acknowledged k ON k.id=p.id").fetchall()],
            "pending_counts": {
                table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                for table in ("supervised_pending", "result_cleanup",
                              "reconciliations", "supervised_expiry")},
        }
    with readonly_database(observations) as db:
        sessions = [
            {"id": row[0], "started": row[1], "stopped": row[2],
             "observed": row[3], "closed_observed": row[4]}
            for row in db.execute(
                "SELECT id,started,stopped,observed,closed_observed "
                "FROM training_sessions ORDER BY started").fetchall()]
        observation_summary = {
            "training_sessions": sessions,
            "active_training_sessions": sum(row["stopped"] is None for row in sessions),
            "draining_training_sessions": sum(
                row["stopped"] is not None and row["closed_observed"] is None
                for row in sessions),
            "last_event": db.execute(
                "SELECT id,kind,recorded_utc FROM events ORDER BY id DESC LIMIT 1").fetchone(),
            "model_updated_utc": db.execute(
                "SELECT updated_utc FROM model WHERE id=1").fetchone(),
        }
    files = [path for database in (worker, observations)
             for path in (database, Path(str(database) + "-wal"),
                          Path(str(database) + "-shm")) if path.exists()]
    opened = subprocess.run(
        ["/usr/sbin/lsof", "-nP"] + [str(path) for path in files],
        capture_output=True, text=True, check=False)
    if opened.returncode not in (0, 1):
        raise RuntimeError("Cannot inspect native SQLite consumers")
    return {
        "captured_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "native_phase_only": True,
        "recheck_required_before_handoff": True,
        "worker": worker_summary,
        "observations": observation_summary,
        "files": [{"path": str(path), "bytes": path.stat().st_size}
                  for path in files],
        "native_open_files": opened.stdout.splitlines(),
    }


def binding(source, target, readonly=False):
    return {"type": "bind", "source": str(source), "target": str(target),
            "read_only": readonly, "bind": {"create_host_path": False}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-manifest", type=Path, required=True)
    parser.add_argument("--controller-config", type=Path, required=True)
    parser.add_argument("--client-directory", type=Path, required=True)
    parser.add_argument("--launch-agents", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--rover-origin", required=True)
    parser.add_argument("--provider-host", required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--gid", type=int, required=True)
    parser.add_argument("--image", default=IMAGE)
    parser.add_argument("--docker", default="/Applications/Docker.app/Contents/Resources/bin/docker")
    args = parser.parse_args()

    if not re.fullmatch(r"sha256:[0-9a-f]{64}", args.image):
        parser.error("--image must be an immutable local image ID")
    if args.output_directory.exists():
        parser.error("Output directory must be new; existing bundles are never overwritten")
    for path in (args.native_manifest, args.controller_config, args.client_directory,
                 args.launch_agents, args.output_directory, args.home):
        if not path.is_absolute():
            parser.error("All host paths must be absolute")
    origin = urlparse(args.rover_origin)
    if origin.scheme not in ("http", "https") or not origin.hostname or origin.username:
        parser.error("Invalid Rover observer origin")
    native = read_object(args.native_manifest)
    for key in ("state", "database"):
        if not Path(native[key]).is_absolute() or not Path(native[key]).exists():
            parser.error("Existing absolute " + key + " path required")
    state = Path(native["state"])
    database_directory = Path(native["database"]).parent
    if (state.stat().st_uid, state.stat().st_gid) != (args.uid, args.gid):
        parser.error("Runtime user must preserve native state ownership")

    client_files = {name: args.client_directory / name for name in
                    ("host-io.json", "host-io.token", "host-io-ca.pem")}
    for path in client_files.values():
        if not path.is_file():
            parser.error("Missing HostIO client file: " + str(path))
    client = read_object(client_files["host-io.json"])
    endpoint = urlparse(client.get("url", ""))
    if endpoint.scheme != "https" or endpoint.port != 8766:
        parser.error("Production HostIO TLS8766 client required")
    for key, expected in (("token_file", "/run/biome/host-io.token"),
                          ("ca_file", "/run/biome/host-io-ca.pem")):
        if client.get(key) != expected:
            parser.error("HostIO client has incompatible " + key)

    environment = dict(os.environ)
    environment["PATH"] = str(Path(args.docker).parent) + os.pathsep + environment.get("PATH", "")
    inspected = subprocess.run(
        [args.docker, "image", "inspect", args.image], check=True,
        capture_output=True, text=True, env=environment)
    image = json.loads(inspected.stdout)[0]
    if image["Id"] != args.image or image["Os"] != "linux":
        parser.error("Qualified Linux image is not present")
    expected_revision = "90cc2f6f6a29f16b2241f65a8e8b243b92545379"
    if image.get("Config", {}).get("Labels", {}).get("org.opencontainers.image.revision") != expected_revision:
        parser.error("Image source revision differs from the qualified motion image")

    plists = []
    for label in LABELS:
        path = args.launch_agents / (label + ".plist")
        data = path.read_bytes()
        decoded = plistlib.loads(data)
        if decoded.get("Label") != label:
            parser.error("Unexpected native launchd label")
        plists.append((label, path, data, decoded))
    controller = read_object(args.controller_config)
    secret = controller.get("stream_router_secret")
    if not isinstance(secret, str) or not secret:
        parser.error("Native controller stream secret is unavailable")
    observer_token = hmac.new(secret.encode(), b"rover-site", hashlib.sha256).hexdigest()
    del secret, controller

    spec = importlib.util.spec_from_file_location("motion_render_config", HERE / "render-config.py")
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
    provider, observer = renderer.render(native, args.rover_origin)
    snapshot = custody_snapshot(native)

    output = args.output_directory
    output.mkdir(mode=0o700, parents=True)
    rollback = output / "rollback"
    rollback.mkdir(mode=0o700)
    write_json(output / "provider.json", provider)
    write_json(output / "observer.json", observer)
    write_private(output / "rover.token", observer_token + "\n")
    del observer_token
    write_private(rollback / "provider.native.json", args.native_manifest.read_bytes())
    saved_plists = []
    for label, source, data, decoded in plists:
        target = rollback / source.name
        write_private(target, data)
        saved_plists.append({
            "label": label, "original": str(source), "backup": str(target),
            "sha256": digest(target),
            "program_arguments": decoded["ProgramArguments"],
            "working_directory": decoded.get("WorkingDirectory"),
        })

    common = {
        "image": args.image, "pull_policy": "never",
        "user": str(args.uid) + ":" + str(args.gid),
        "environment": {"HOME": str(args.home), "BIOME_PROVIDER_HOST": args.provider_host},
        "restart": "unless-stopped", "init": True, "stop_grace_period": "60s",
    }
    host_io_mounts = [
        binding(path, "/run/biome/" + name, True)
        for name, path in client_files.items()]
    services = {
        "motion-observer": {
            **common, "command": ["observer", "--config", "/config/observer.json"],
            "volumes": [binding(output / "observer.json", "/config/observer.json", True),
                        *host_io_mounts,
                        binding(output / "rover.token", "/run/biome/rover.token", True),
                        binding(database_directory, database_directory)]},
        "motion-provider": {
            **common, "command": ["provider", "--config", "/config/provider.json"],
            "volumes": [binding(output / "provider.json", "/config/provider.json", True),
                        *host_io_mounts, binding(state, state),
                        binding(database_directory, database_directory)]},
    }
    compose = output / "compose.json"
    write_json(compose, {"name": "biomerte-motion", "services": services})
    validated = subprocess.run(
        [args.docker, "compose", "-f", str(compose), "config", "--quiet"],
        capture_output=True, text=True, env=environment)
    if validated.returncode:
        raise RuntimeError("Prepared Compose configuration was rejected: " + validated.stderr[:1000])
    receipt = {
        "status": "prepared_not_started",
        "prepared_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "bundle_directory": str(output),
        "image": {key: image.get(key) for key in ("Id", "Architecture", "Os", "Size")},
        "image_source_revision": expected_revision,
        "identity": {key: native[key] for key in ("provider", "site", "resource", "slots")},
        "placement": {"uid": args.uid, "gid": args.gid,
                      "home": str(args.home), "provider_host": args.provider_host},
        "preserved_in_place": {"provider_state": str(state),
                               "observations": native["database"],
                               "worker_ledger": str(state / "worker.sqlite")},
        "native_manifest": {"path": str(args.native_manifest),
                            "sha256": digest(args.native_manifest)},
        "rollback_plists": saved_plists,
        "host_io": {"client_directory": str(args.client_directory),
                    "url": client["url"],
                    "credentials": "existing client files referenced; never embedded"},
        "rover": {"origin": args.rover_origin,
                  "token": "private HMAC derivation from existing stream_router_secret"},
        "offline_validation": {"compose_config": "passed", "image_inspect": "passed"},
        "network_preflight": "not_run_by_prepare; dependency and custody checks required before handoff",
        "custody_snapshot": snapshot,
        "activation": "Root-controlled: stop and disable native owners before either container starts",
        "rollback": "Stop both containers before restoring native ownership; retain current state/WAL",
    }
    write_json(output / "receipt.json", receipt)
    print(json.dumps({"status": receipt["status"], "bundle_directory": str(output),
                      "image": args.image, "compose_config": "passed",
                      "active_training_sessions": snapshot["observations"]["active_training_sessions"],
                      "draining_training_sessions": snapshot["observations"]["draining_training_sessions"],
                      "recovery_result_count": len(snapshot["worker"]["recovery_results"])}))


if __name__ == "__main__":
    main()
