#!/usr/bin/env python3
"""Install the Docker service supervisor without modifying native services."""
from __future__ import print_function
import argparse
import hashlib
import hmac
import json
import math
import os
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit, urlunsplit


def command(argv, capture=False):
    if capture:
        return subprocess.check_output(argv).decode("utf-8").strip()
    subprocess.check_call(argv)


def private_json(path, value):
    tmp = path + ".new"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(value, out, sort_keys=True, indent=2)
        out.write("\n")
    os.replace(tmp, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--controller-home", required=True, help="Existing controller home containing node.json")
    parser.add_argument("--config-dir", required=True, help="Private persistent supervisor configuration directory")
    parser.add_argument("--state-dir", required=True, help="Local supervisor SQLite directory")
    parser.add_argument("--manifest", action="append", default=[], help="Initial full service manifest (may repeat)")
    parser.add_argument("--base-image", default="python:3.11-alpine")
    parser.add_argument("--docker", default=shutil.which("docker") or "/usr/local/bin/docker")
    parser.add_argument("--container-name", default="biomerte-service-runner")
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--docker-request-timeout-s", type=float, default=30)
    parser.add_argument("--host-settings", help="Private JSON containing bindings, adapters, and port_overrides")
    args = parser.parse_args()
    if not math.isfinite(args.docker_request_timeout_s) or args.docker_request_timeout_s <= 0:
        raise ValueError("Docker request timeout must be positive and finite")
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    controller_home = os.path.abspath(os.path.expanduser(args.controller_home))
    config_dir = os.path.abspath(os.path.expanduser(args.config_dir))
    state_dir = os.path.abspath(os.path.expanduser(args.state_dir))
    with open(os.path.join(controller_home, "node.json")) as source:
        node = json.load(source)
    worker_id = node["node"]
    origin = urlsplit(node["stream_gateway_url"])
    if origin.scheme not in ("https", "wss") or not origin.hostname or origin.username or origin.password:
        raise ValueError("Existing controller must name its authenticated public stream gateway")
    coordinator_url = urlunsplit(("https", origin.netloc, "", "", ""))
    secret = node["stream_router_secret"]
    if not isinstance(secret, str) or len(secret) < 32:
        raise ValueError("Existing stream router secret is unavailable")
    command([args.docker, "version", "--format", "{{.Server.Version}}"])
    for path in (config_dir, state_dir, os.path.join(config_dir, "services")):
        if not os.path.isdir(path):
            os.makedirs(path)
        os.chmod(path, 0o700)
    key_path = os.path.join(config_dir, "coordinator.key")
    key = hmac.new(secret.encode(), b"biomerte/service-coordinator/v1", hashlib.sha256).hexdigest()
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write(key + "\n")
    os.chmod(key_path, 0o600)
    manifest_paths = []
    import sys
    sys.path.insert(0, root)
    from biomerte.docker_services import validate_manifest
    for source_path in args.manifest:
        with open(source_path) as source:
            manifest = validate_manifest(json.load(source))
        target = os.path.join(config_dir, "services", manifest["id"] + ".json")
        private_json(target, manifest)
        manifest_paths.append("/config/services/" + manifest["id"] + ".json")
    configuration = {"worker_id": worker_id, "coordinator_url": coordinator_url,
                     "coordinator_key_file": "/config/coordinator.key",
                     "docker_socket": "/var/run/docker.sock", "manifest_files": manifest_paths,
                     "state_file": "/state/current.sqlite", "heartbeat_interval_s": 1,
                     "request_timeout_s": 3, "docker_request_timeout_s": args.docker_request_timeout_s}
    if args.host_settings:
        with open(args.host_settings) as source:
            host_settings = json.load(source)
        if not isinstance(host_settings, dict) or set(host_settings) - {"bindings", "adapters", "port_overrides"}:
            raise ValueError("Host settings may contain only bindings, adapters, and port_overrides")
        if any(not isinstance(value, dict) for value in host_settings.values()):
            raise ValueError("Each host settings section must be an object")
        configuration.update(host_settings)
    private_json(os.path.join(config_dir, "runner.json"), configuration)
    context = tempfile.mkdtemp(prefix="biomerte-runner-build-")
    try:
        os.makedirs(os.path.join(context, "biomerte"))
        for module in ("docker_services.py", "service_adapters.py"):
            shutil.copyfile(os.path.join(root, "biomerte", module),
                            os.path.join(context, "biomerte", module))
        shutil.copyfile(os.path.join(root, "deploy", "docker-services", "Dockerfile"),
                        os.path.join(context, "Dockerfile"))
        command([args.docker, "build", "--build-arg", "PYTHON_IMAGE=" + args.base_image,
                 "-t", "biomerte-service-runner:local", context])
    finally:
        shutil.rmtree(context)
    image_id = command([args.docker, "image", "inspect", "--format", "{{.Id}}",
                        "biomerte-service-runner:local"], capture=True)
    print(json.dumps({"worker_id": worker_id, "image_id": image_id,
                      "config_dir": config_dir, "state_dir": state_dir,
                      "coordinator_url": coordinator_url, "initial_services": len(manifest_paths)}))
    if args.build_only:
        return
    # Replace only the explicitly named supervisor when it is already ours.
    found = subprocess.Popen([args.docker, "inspect", args.container_name],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output, _ = found.communicate()
    if found.returncode == 0:
        previous = json.loads(output.decode())[0]
        if previous.get("Config", {}).get("Labels", {}).get("org.biomerte.service.supervisor") != worker_id:
            raise RuntimeError("Container name belongs to another service; nothing stopped")
        command([args.docker, "rm", "-f", args.container_name])
    command([args.docker, "run", "-d", "--name", args.container_name,
             "--restart", "unless-stopped",
             "--label", "org.biomerte.service.supervisor=" + worker_id,
             "--log-driver", "json-file", "--log-opt", "max-size=8m", "--log-opt", "max-file=3",
             "-v", "/var/run/docker.sock:/var/run/docker.sock",
             "-v", config_dir + ":/config:ro",
             "-v", state_dir + ":/state",
             image_id])
    print("Supervisor started. Qualify readiness and each intended standby before declaring failover available.")


if __name__ == "__main__":
    main()
