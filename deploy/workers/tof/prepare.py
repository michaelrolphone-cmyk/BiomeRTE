#!/usr/bin/env python3
"""Render an actual ToF worker deployment; never stop/start a live service."""
import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
import plistlib
import secrets
import shutil
import socket
import subprocess


HOME = Path.home()
DATA = HOME / ".local/share"
TEMPLATE = Path(__file__).with_name("compose.template.json")
STATE_FILES = {
    "tof": ["baseline.json", "geometry-config.json", "display-floor-priors.json",
            "anchor-registry.json", "rover-refinement.json", "latest.json",
            "request-custody.json"],
    "vision": ["config.json", "camera-rig.json", "survey-world.json", "rover-motion.json",
               "survey-pose-zero.json", "survey-heading-state.json",
               "survey-magnetic-north.json", "dual-radio-survey-calibration.json",
               "rover-world-body-pose.json"],
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    path = Path(path)
    content = json.dumps(value, sort_keys=True, indent=2) + "\n"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as target:
        target.write(content)


def private_copy(source, target):
    source, target = Path(source), Path(target)
    if not source.is_file() or source.is_symlink():
        raise ValueError("Required private/configuration source is not a regular file")
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(source.read_bytes())


def option(argv, name):
    index = argv.index(name)
    return argv[index + 1]


def render(value, values):
    if isinstance(value, dict):
        return {k: render(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, values) for v in value]
    if isinstance(value, str):
        for key, item in values.items():
            value = value.replace("@" + key + "@", str(item))
        if "@" in value:
            raise ValueError("Unresolved compose placeholder")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Qualified local image ID or immutable digest")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--docker", default="/usr/local/bin/docker")
    parser.add_argument("--tof-state", type=Path, default=DATA/"biome-tof")
    parser.add_argument("--vision-state", type=Path, default=DATA/"biome-robot-vision")
    parser.add_argument("--native-plist", type=Path,
                        default=HOME/"Library/LaunchAgents/org.biomerte.tof-coordinator.plist")
    parser.add_argument("--bridge-plist", type=Path,
                        default=HOME/"Library/LaunchAgents/org.biomerte.host-io.plist")
    parser.add_argument("--bridge-config", type=Path,
                        default=DATA/"biomerte-host-io/config/server.active.json")
    parser.add_argument("--bridge-url", default="http://host.docker.internal:8765")
    parser.add_argument("--bridge-ca", type=Path, help="Native CA file required for an HTTPS HostIO bridge")
    parser.add_argument("--health-port", type=int, default=18771)
    parser.add_argument("--survey-port", type=int, default=18772)
    args = parser.parse_args()
    if not (args.image.startswith("sha256:") or "@sha256:" in args.image):
        parser.error("--image must be an immutable image ID/digest")
    inspected = json.loads(subprocess.check_output(
        [args.docker, "image", "inspect", args.image], text=True))[0]
    if (inspected["Os"], inspected["Architecture"]) != ("linux", "arm64"):
        raise ValueError("This deployment requires the qualified Linux ARM64 image")
    labels = inspected.get("Config", {}).get("Labels", {}) or {}
    if labels.get("io.biomerte.image") != "tof-coordinator":
        raise ValueError("Selected image is not the actual ToF worker")
    native = plistlib.loads(args.native_plist.read_bytes())
    bridge_plist = plistlib.loads(args.bridge_plist.read_bytes())
    argv = native["ProgramArguments"]
    if Path(argv[1]).name != "tof_coordinator.py":
        raise ValueError("Unexpected native ToF launch command")
    if Path(option(argv, "--state")).resolve() != args.tof_state.resolve():
        raise ValueError("Selected state directory differs from the live native declaration")
    camera_id = option(argv, "--camera-id")
    for path in (args.tof_state, args.vision_state):
        if not path.is_dir():
            raise ValueError("Existing native state directory is required")
    for port in (args.health_port, args.survey_port):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
    original = json.loads(args.bridge_config.read_text())
    if Path(original["vision_directory"]).resolve() != args.vision_state.resolve():
        raise ValueError("Bridge and worker vision state must refer to the same directory")
    out = args.output.resolve()
    out.mkdir(parents=True, mode=0o700, exist_ok=False)
    for name in ("config", "private", "rollback"):
        (out/name).mkdir(mode=0o700)
    private_copy(args.native_plist, out/"rollback/native.plist")
    private_copy(args.bridge_plist, out/"rollback/bridge.plist")
    private_copy(args.bridge_config, out/"rollback/bridge.json")
    private_copy(original["token_file"], out/"private/host-io.token")
    token = out/"private/survey-control.token"
    fd = os.open(str(token), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd,"w") as output:
        output.write(secrets.token_urlsafe(48) + "\n")
    scheme = urlsplit(args.bridge_url).scheme
    if scheme not in ("http", "https"):
        raise ValueError("HostIO bridge must use HTTP or HTTPS")
    client = dict(url=args.bridge_url, allow_local_http=scheme == "http",
                  host_id=original["host_id"], token_file="/private/host-io.token")
    if scheme == "https":
        if args.bridge_ca is None:
            raise ValueError("HTTPS HostIO requires --bridge-ca")
        private_copy(args.bridge_ca, out/"config/host-io-ca.pem")
        client["ca_file"] = "/config/host-io-ca.pem"
    write(out/"config/host-io.json", client)
    activated = dict(original)
    activated["anchors"] = dict(original.get("anchors", {}), enabled=True)
    activated["enable_effects"] = True
    activated["survey_control"] = dict(url="http://127.0.0.1:" + str(args.survey_port),
        allow_local_http=True, token_file=str(token))
    write(out/"config/bridge.active.json", activated)
    write(out/"config/bridge.patch.json", dict(anchors=dict(enabled=True),
        enable_effects=True, survey_control=activated["survey_control"]))
    values = dict(IMAGE=inspected["Id"], USER=str(os.getuid())+":"+str(os.getgid()),
                  TOF_STATE=args.tof_state.resolve(), VISION_STATE=args.vision_state.resolve(),
                  CONFIG=out/"config", PRIVATE=out/"private", CAMERA_ID=camera_id,
                  HEALTH_PORT=args.health_port, SURVEY_PORT=args.survey_port)
    compose = render(json.loads(TEMPLATE.read_text()), values)
    write(out/"compose.json", compose)
    plan = dict(schema="biomerte.tof-direct-deployment/1", image_id=inspected["Id"],
        source_revision=labels.get("org.opencontainers.image.revision"),
        platform="linux/arm64", prepared_only=True, same_host_state=True,
        lock_scope="container-only; native PID stop and launchd disablement required",
        state_paths=dict(tof=str(args.tof_state.resolve()),vision=str(args.vision_state.resolve())),
        native=dict(label=native["Label"],plist=str(args.native_plist),
                    program=argv[1],plist_sha256=sha(args.native_plist)),
        bridge=dict(label=bridge_plist["Label"],plist=str(args.bridge_plist),
                    config=str(args.bridge_config),before_sha256=sha(args.bridge_config),
                    proposed_sha256=sha(out/"config/bridge.active.json")),
        camera_id=camera_id, health_url="http://127.0.0.1:"+str(args.health_port),
        state_files=STATE_FILES, uid=os.getuid(),
        compose_command=[args.docker,"compose","-p","biome-tof","-f",str(out/"compose.json")],
        qualifications_required=["active-image-fixture","previous-native-owner-stopped",
                                 "bridge-active","actual-readyz","native-reader-state-fresh"])
    write(out/"plan.json",plan)
    print(json.dumps(dict(output=str(out),image_id=plan["image_id"],
                          source_revision=plan["source_revision"],
                          activated=False,registry_used=False)))


if __name__=="__main__":
    main()
