#!/usr/bin/env python3
"""Review or install two user LaunchAgents for continuous robot perception."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import socket
import subprocess
import sys
import tempfile
import time

from setup_macos_app import launch_agent_spec

HERE = Path(__file__).resolve().parent
DEFAULT_STATE = Path.home() / ".local/share/biome-robot-vision"
DEFAULT_GATEWAY = Path.home() / ".local/share/biome-mac-terminal"
SERVICE_LABEL = "biome.robot-vision"
PROVIDER_LABEL = "biome.robot-vision-provider"


def make_config(repo, state, gateway, terminal_manifest, terminal_identity, slots=4, previous=None, service_mode=None):
    previous = previous or {}
    service_mode = service_mode or previous.get("service_mode", "python")
    if service_mode not in {"python", "macos-app"}:
        raise ValueError("service_mode must be python or macos-app")
    if not 1 <= slots <= 16:
        raise ValueError("Expected 1–16 concurrent query slots.")
    repo, state, gateway = [Path(p).expanduser().resolve() for p in (repo, state, gateway)]
    site, host_node = terminal_manifest["site"], terminal_identity["node"]
    provider = hashlib.sha256(("biomerte/mac/robot-vision/v1/" + host_node).encode()).hexdigest()
    resource = "robotvision.mac." + host_node[:20]
    if previous and (previous.get("site") != site or previous.get("provider") != provider or
                     previous.get("resource") != resource):
        raise ValueError("Existing provider identity changed; preserve and reconcile its ledger first.")
    return dict(version=1, site=site, provider=provider, resource=resource,
                host=socket.gethostname(), repo=str(repo), state=str(state),
                gateway_state=str(gateway), socket=str(state / "vision.sock"),
                python=str(state / "env/bin/python"), slots=slots, service_mode=service_mode,
                query_timeout_seconds=None, worker_timeout_seconds=None, health_timeout_seconds=5,
                poll_seconds=2, health_seconds=10)


def launch_agents(cfg, home=None):
    home = Path(home or Path.home())
    state, repo = Path(cfg["state"]), Path(cfg["repo"])
    environment = {
        "HOME": str(home),
        "PATH": str(state / "env/bin") + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "PYTHONUNBUFFERED": "1",
    }
    programs = [
        (SERVICE_LABEL, [cfg["python"], str(repo / "providers/robot-vision/vision_service.py"),
                         "--state", str(state)], "vision"),
        (PROVIDER_LABEL, [cfg["python"], str(repo / "providers/robot-vision/biome_provider.py"),
                          "serve", "--state", str(state)], "provider"),
    ]
    jobs = {
        label: dict(Label=label, ProgramArguments=argv, WorkingDirectory=str(state),
                    RunAtLoad=True, KeepAlive=True, ThrottleInterval=10, ExitTimeOut=0,
                    EnvironmentVariables=dict(environment),
                    StandardOutPath=str(state / (log + ".log")),
                    StandardErrorPath=str(state / (log + "-error.log")))
        for label, argv, log in programs
    }
    mode = cfg.get("service_mode", "python")
    if mode == "macos-app":
        jobs[SERVICE_LABEL] = launch_agent_spec(
            state, app_path=home / "Applications/Biome Robot Vision.app",
            label=SERVICE_LABEL, require_installed=False)
    elif mode != "python":
        raise ValueError("service_mode must be python or macos-app")
    return jobs


def atomic_write(path, data, mode=0o600):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_install(cfg, home=None):
    if sys.platform != "darwin":
        raise RuntimeError("This installer requires a macOS user session.")
    if not os.access(cfg["python"], os.X_OK):
        raise RuntimeError("Create the perception environment at STATE/env before installation.")
    for relative in ("providers/robot-vision/vision_service.py", "providers/robot-vision/biome_provider.py"):
        if not (Path(cfg["repo"]) / relative).is_file():
            raise RuntimeError("The selected repository is missing " + relative)
    for relative in ("mac_provider.py", "oauth.json"):
        if not (Path(cfg["gateway_state"]) / relative).is_file():
            raise RuntimeError("An authorized mac-terminal gateway installation is required.")
    if cfg.get("service_mode", "python") == "macos-app":
        launch_agent_spec(
            Path(cfg["state"]),
            app_path=Path(home or Path.home()) / "Applications/Biome Robot Vision.app",
            label=SERVICE_LABEL, require_installed=True)


def install(cfg, start=False, home=None):
    """No new OAuth flow, system daemon, copied credential, or camera reconfiguration."""
    validate_install(cfg, home=home)
    home, state = Path(home or Path.home()), Path(cfg["state"])
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    state.chmod(0o700)
    (state / "provider").mkdir(mode=0o700, exist_ok=True)
    (state / "provider").chmod(0o700)
    agents = home / "Library/LaunchAgents"
    agents.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write(state / "provider-manifest.json",
                 (json.dumps(cfg, indent=2) + "\n").encode())
    jobs = launch_agents(cfg, home)
    for label, spec in jobs.items():
        atomic_write(agents / (label + ".plist"), plistlib.dumps(spec))
    if start:
        target = "gui/" + str(os.getuid())
        subprocess.run(["/bin/launchctl", "print", target], capture_output=True, check=True)
        for label in (SERVICE_LABEL, PROVIDER_LABEL):
            job = target + "/" + label
            replacing = subprocess.run(["/bin/launchctl", "print", job], capture_output=True).returncode == 0
            if replacing:
                subprocess.run(["/bin/launchctl", "bootout", job], capture_output=True, check=True)
            subprocess.run(["/bin/launchctl", "enable", job], capture_output=True, check=True)
            deadline = time.monotonic() + 30
            while True:
                result = subprocess.run(["/bin/launchctl", "bootstrap", target,
                                         str(agents / (label + ".plist"))], capture_output=True)
                if result.returncode == 0:
                    break
                if not replacing or result.returncode != 5 or time.monotonic() >= deadline:
                    result.check_returncode()
                time.sleep(1)
    return {"installed": True, "started": start, "resource": cfg["resource"],
            "labels": list(jobs), "state": cfg["state"]}


def stop():
    if sys.platform != "darwin":
        raise RuntimeError("This command requires macOS.")
    target = "gui/" + str(os.getuid())
    stopped = []
    for label in (PROVIDER_LABEL, SERVICE_LABEL):
        job = target + "/" + label
        if subprocess.run(["/bin/launchctl", "print", job], capture_output=True).returncode == 0:
            subprocess.run(["/bin/launchctl", "bootout", job], capture_output=True, check=True)
            stopped.append(label)
    return {"stopped": stopped}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(HERE.parents[1]))
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--gateway-state", default=str(DEFAULT_GATEWAY))
    parser.add_argument("--slots", type=int)
    parser.add_argument("--service-mode", choices=["python", "macos-app"],
                        help="Explicit camera-service mode; preserve the previous selection when omitted.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--install", action="store_true", help="Write manifest and user LaunchAgents.")
    mode.add_argument("--stop", action="store_true", help="Stop these two user jobs; retain maps and state.")
    parser.add_argument("--start", action="store_true", help="With --install, start/restart both user jobs.")
    args = parser.parse_args(argv)
    if args.start and not args.install:
        parser.error("--start requires --install")
    if args.stop:
        print(json.dumps(stop(), indent=2))
        return 0
    state, gateway = Path(args.state).expanduser().resolve(), Path(args.gateway_state).expanduser().resolve()
    manifest_path = state / "provider-manifest.json"
    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    terminal_manifest = json.loads((gateway / "terminal-manifest.json").read_text())
    terminal_identity = json.loads((gateway / "identity/node.json").read_text())
    cfg = make_config(args.repo, state, gateway, terminal_manifest, terminal_identity,
                      args.slots if args.slots is not None else previous.get("slots", 4), previous,
                      service_mode=args.service_mode)
    if args.install:
        output = install(cfg, args.start)
    else:
        output = {"installed": False, "plan": "Use --install to write files; add --start to launch.",
                  "configuration": cfg, "launch_agents": launch_agents(cfg)}
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
