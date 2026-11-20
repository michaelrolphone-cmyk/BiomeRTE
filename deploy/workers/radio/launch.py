#!/usr/bin/env python3
"""Render or run explicit Pi radio container configuration; never stops native services."""
import argparse
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess

IMAGE = re.compile(r"^sha256:[0-9a-f]{64}$")
USB = re.compile(r"^/dev/bus/usb/[0-9]{3}/[0-9]{3}$")
SERIAL = re.compile(r"^/dev/tty(?:USB|ACM)[0-9]+$")
NATIVE = {"sdr": ["biomerte-sdr-provider.service", "biomerte-sdr.service"],
          "fm": ["biomerte-fm-provider.service", "biomerte-fm.service"]}

def execute(argv):
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate()
    return p.returncode, out.decode("utf8", "replace"), err.decode("utf8", "replace")

def validate(config):
    if config.get("kind") not in NATIVE or not IMAGE.fullmatch(config.get("image", "")):
        raise ValueError("kind and a qualified immutable local image ID are required")
    kind = config["kind"]
    expected_home = "/home/pi/.local/share/biomerte-" + kind
    expected_state = "/home/pi/.local/share/" + ("sdr-receiver" if kind == "sdr" else "fm-transmitter")
    if config.get("home") != expected_home or config.get("state") != expected_state:
        raise ValueError("Explicit existing Pi provider/state paths are required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", config.get("hostname", "")):
        raise ValueError("Native execution hostname is required")
    if not isinstance(config.get("devices", []), list):
        raise ValueError("devices must be an array")
    if kind == "sdr":
        if not config.get("devices"):
            raise ValueError("Explicit receiver USB/serial devices are required")
        if any(not isinstance(p, str) or not (USB.fullmatch(p) or SERIAL.fullmatch(p)) for p in config["devices"]):
            raise ValueError("Only exact USB bus nodes and receiver ttyUSB/ttyACM nodes may be mapped")
    elif config.get("devices"):
        raise ValueError("FM has one fixed /dev/mem mapping; extra devices are not accepted")
    groups = config.get("device_groups", [])
    if any(isinstance(g, bool) or not isinstance(g, int) or g < 0 for g in groups):
        raise ValueError("Device groups must be numeric host GIDs")
    audio = config.get("audio_directory")
    if audio and (kind != "fm" or not isinstance(audio, str) or not audio.startswith("/home/pi/") or ".." in Path(audio).parts):
        raise ValueError("Optional audio directory must be within /home/pi")
    return config

def argv(config, docker="/usr/bin/docker"):
    c = validate(config)
    kind = c["kind"]
    result = [docker, "run", "--rm", "--name", "biomerte-worker-" + kind,
              "--hostname", c["hostname"], "--network", "host", "--read-only",
              "--cap-drop", "ALL", "--stop-timeout", "30",
              "--log-driver", "json-file", "--log-opt", "max-size=1m", "--log-opt", "max-file=3",
              "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m,mode=1777",
              "--tmpfs", "/run/biomerte:rw,noexec,nosuid,size=4m,uid=1000,gid=1000,mode=0700",
              "-v", c["home"] + ":" + c["home"] + ":rw",
              "-v", c["state"] + ":" + c["state"] + ":rw"]
    if kind == "sdr":
        result += ["--security-opt", "no-new-privileges"]
        for device in c["devices"]:
            result += ["--device", device + ":" + device + ":rw"]
        for gid in c.get("device_groups", []):
            result += ["--group-add", str(gid)]
    else:
        for cap in ("SYS_RAWIO", "SETUID", "SETGID", "DAC_OVERRIDE", "AUDIT_WRITE"):
            result += ["--cap-add", cap]
        result += ["--device", "/dev/mem:/dev/mem:rw",
                   "-v", "/sys/firmware/devicetree/base:/run/pi-device-tree:ro",
                   "-v", "/run/lock/capabilities-fm-transmit.lock:/run/lock/capabilities-fm-transmit.lock:rw"]
        if c.get("audio_directory"):
            result += ["-v", c["audio_directory"] + ":" + c["audio_directory"] + ":ro"]
    return result + [c["image"], "serve", "--home", c["home"], "--state", c["state"]]

def check_host(c, docker):
    validate(c)
    if platform.system() != "Linux" or platform.machine() != "armv7l":
        raise ValueError("This rollout is for the inspected ARMv7 Pi")
    import pwd
    account = pwd.getpwnam("pi")
    if account.pw_uid != 1000 or account.pw_gid != 1000:
        raise ValueError("Image user must match the existing Pi state owner")
    for unit in NATIVE[c["kind"]]:
        unused, status, unused_error = execute(["systemctl", "is-active", unit])
        if status.strip() in ("active", "activating", "reloading"):
            raise ValueError("Native service still owns the worker: " + unit)
    if not (Path(c["home"]) / "config.json").is_file() or not (Path(c["home"]) / "manifest.json").is_file():
        raise ValueError("Existing provider enrollment is missing")
    if not Path(c["state"]).is_dir():
        raise ValueError("Existing worker state is missing")
    for device in c.get("devices", []):
        if not stat.S_ISCHR(os.stat(device).st_mode):
            raise ValueError("Receiver device is not a character device: " + device)
    if c["kind"] == "fm":
        with open("/sys/firmware/devicetree/base/model", "rb") as stream:
            model = stream.read().rstrip(b"\0").decode("ascii")
        if model != "Raspberry Pi 3 Model B Rev 1.2":
            raise ValueError("FM source is qualified for the existing Pi 3 Model B Rev 1.2")
        if not stat.S_ISCHR(os.stat("/dev/mem").st_mode):
            raise ValueError("/dev/mem is unavailable")
        lock = Path("/run/lock/capabilities-fm-transmit.lock")
        if not lock.is_file() or lock.is_symlink():
            raise ValueError("Existing shared native FM exclusion lock is required")
        for path in Path(c["state"]).glob("job-*/state.json"):
            with path.open() as handle:
                record = json.load(handle)
            if record.get("state") not in ("completed", "stopped", "interrupted", "failed"):
                raise ValueError("Reconcile the existing FM job before handoff: " + str(path))
    code, output, error = execute([docker, "image", "inspect", c["image"]])
    if code:
        raise ValueError("Load the qualified image locally before activation")
    image = json.loads(output)[0]
    if image.get("Os") != "linux" or image.get("Architecture") != "arm":
        raise ValueError("Local image does not match the Pi architecture")

def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=("plan", "check-host", "run"))
    p.add_argument("--config", required=True)
    p.add_argument("--docker", default="/usr/bin/docker")
    a = p.parse_args()
    with open(a.config) as handle:
        c = json.load(handle)
    command = argv(c, a.docker)
    if a.action == "plan":
        print(json.dumps({"argv": command, "native_units": NATIVE[c["kind"]],
                          "identity_reused": True, "hardware_qualified": False}, indent=2))
        return
    check_host(c, a.docker)
    if a.action == "run":
        os.execv(a.docker, command)
    print(json.dumps({"ready_for_explicit_activation": True, "hardware_opened": False}))

if __name__ == "__main__":
    main()
