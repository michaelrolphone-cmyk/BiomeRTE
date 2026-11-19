#!/usr/bin/env python3
"""Produce the existing runner schema with immutable images and logical bindings."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from biomerte.docker_services import revision, validate_manifest

def router_manifest(images, *, service_id="stream-router", network="biomerte-services", host_port=8751):
    if type(host_port) is not int or not 1 <= host_port <= 65535:
        raise ValueError("Invalid host listener port")
    result = {
        "id": service_id, "images": dict(images), "effect_policy": "stateless", "state_policy": "stateless",
        "entrypoint": ["python"], "command": ["/app/router.py", "--config", "/config/router.json"],
        "mounts": [{"binding": service_id + "-private", "target": "/config", "role": "private", "read_only": True}],
        "network": network, "ports": [{"container": 8751, "host": host_port, "listen": "127.0.0.1", "protocol": "tcp"}],
        "health": {"command": ["python", "/app/probe.py"], "interval_s": 5, "timeout_s": 4, "retries": 3},
        "read_only_root": True, "restart_policy": "no",
        "log_max_size": "8m", "log_max_files": 3,
        "lease_ttl_s": 15, "worker_ttl_s": 20, "startup_grace_s": 60
    }
    result["revision"] = revision(result)
    return validate_manifest(result)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", action="append", required=True, help="Docker platform=immutable image reference")
    parser.add_argument("--service-id", default="stream-router")
    parser.add_argument("--network", default="biomerte-services")
    parser.add_argument("--host-port", type=int, default=8751)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    images = dict(value.split("=", 1) for value in args.image)
    value = router_manifest(images, service_id=args.service_id, network=args.network, host_port=args.host_port)
    Path(args.output).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")

if __name__ == "__main__":
    main()
