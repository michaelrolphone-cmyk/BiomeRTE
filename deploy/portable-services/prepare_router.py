#!/usr/bin/env python3
"""Prepare private Router configuration without opening a controller database.

This reads existing identities/secrets and writes a new private candidate
directory. It never changes installed configuration, launchd or Docker.
"""
import argparse
import json
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "source"))
from portable_common import endpoint, read_json
from router import configure

def prepared_config(native, nodes, *, owner_inspect_url=None, native_inspect_url=None,
                    allow_owner_local_http=False, port=8751):
    if bool(owner_inspect_url) == bool(native_inspect_url):
        raise ValueError("Select exactly one existing owner-bridge or HTTPS authority inspection endpoint")
    result = {
        "transport": "private_docker_http", "network_scope": "private", "listen": "0.0.0.0",
        "port": port, "control_ntp": native.get("control_ntp", True),
        "peer_status": native.get("peer_status", True), "host_io_required": True,
        "host_io_config": "/config/host-io.json",
        "remote_controllers": dict(native.get("remote_controllers", {}))
    }
    for key in ("service_authority_secret", "service_inspect_url"):
        if native.get(key):
            result[key] = native[key]
    if owner_inspect_url:
        endpoint(owner_inspect_url, local_http=allow_owner_local_http, path="/internal/stream/inspect")
        result["owner_bridge_controllers"] = {}
    else:
        endpoint(native_inspect_url)
    for node in nodes:
        authority, secret = node["node"], node["stream_router_secret"]
        if authority in result["remote_controllers"]:
            raise ValueError("Native and remote authority configuration overlap")
        if owner_inspect_url:
            result["owner_bridge_controllers"][authority] = {
                "secret": secret, "inspect_url": owner_inspect_url,
                "allow_local_http": allow_owner_local_http}
        else:
            result["remote_controllers"][authority] = {
                "secret": secret, "inspect_url": native_inspect_url}
    configure(result)
    return result

def private_write(path, raw):
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as target:
        target.write(raw)
        target.flush()
        os.fsync(target.fileno())

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-config", required=True)
    parser.add_argument("--output-dir", required=True)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--owner-inspect-url")
    choice.add_argument("--native-inspect-url")
    parser.add_argument("--allow-owner-local-http", action="store_true")
    parser.add_argument("--host-io-url", required=True)
    parser.add_argument("--host-io-token-file", required=True)
    parser.add_argument("--host-id", required=True)
    parser.add_argument("--allow-host-local-http", action="store_true")
    parser.add_argument("--host-io-ca-file")
    parser.add_argument("--port", type=int, default=8751)
    args = parser.parse_args()
    native = read_json(args.source_config)
    nodes = [read_json(Path(directory) / "node.json") for directory in native.get("controllers", ())]
    config = prepared_config(native, nodes, owner_inspect_url=args.owner_inspect_url,
        native_inspect_url=args.native_inspect_url, allow_owner_local_http=args.allow_owner_local_http,
        port=args.port)
    endpoint(args.host_io_url, local_http=args.allow_host_local_http, path="")
    token = Path(args.host_io_token_file).read_bytes().strip()
    if len(token) < 32:
        raise ValueError("Host I/O credential must contain at least 32 bytes")
    host = {"url": args.host_io_url, "token_file": "/config/host-io.key",
            "allow_local_http": args.allow_host_local_http, "host_id": args.host_id,
            "timeout_s": 3}
    if args.host_io_ca_file:
        host["ca_file"] = "/config/host-io-ca.pem"
    destination = Path(args.output_dir)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    private_write(destination / "host-io.key", token + b"\n")
    if args.host_io_ca_file:
        private_write(destination / "host-io-ca.pem", Path(args.host_io_ca_file).read_bytes())
    private_write(destination / "host-io.json", (json.dumps(host, sort_keys=True, indent=2) + "\n").encode())
    private_write(destination / "router.json", (json.dumps(config, sort_keys=True, indent=2) + "\n").encode())
    print(json.dumps({"state": "prepared", "output_directory": str(destination),
        "authority_count": len(config["remote_controllers"]) + len(config.get("owner_bridge_controllers", {})),
        "native_databases_read": False, "installed_configuration_changed": False}))

if __name__ == "__main__":
    main()
