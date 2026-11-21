#!/usr/bin/env python3
"""Run the actual radio daemon and its existing enrolled capability provider."""
import argparse
import copy
import ctypes.util
import importlib
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
KINDS = ("sdr", "fm")

def rewrite_manifest(manifest, kind, home):
    if kind not in KINDS or not isinstance(manifest, dict):
        raise ValueError("Expected an enrolled radio manifest")
    expected = {"radio.sdr.receive:1", "radio.sdr.receive:2"} if kind == "sdr" else {"radio.fm.transmit:1"}
    offers = manifest.get("offers")
    if not isinstance(offers, list) or not offers:
        raise ValueError("Existing provider offers are required")
    value = copy.deepcopy(manifest)
    for offer in value["offers"]:
        if offer.get("contract") not in expected or not offer.get("resource"):
            raise ValueError("Manifest contains a different capability or no resource")
        offer["argv"] = [sys.executable, str(ROOT / "providers" / ("pi-" + kind) / "server.py"), "call", str(home)]
    return value

def check(kind):
    source = ROOT / "providers" / ("pi-" + kind)
    sys.path.insert(0, str(source))
    for name in (("main", "server", "routing", "capture", "esp_capture") if kind == "sdr" else ("common", "main", "server", "worker", "hardware")):
        importlib.import_module(name)
    result = {"kind": kind, "implementation": str(source), "hardware_opened": False,
              "provider_started": False, "live_qualified": False}
    if kind == "sdr":
        import numpy
        import serial
        libraries = {name: ctypes.util.find_library(name) for name in ("rtlsdr", "usb-1.0")}
        if not all(libraries.values()):
            raise RuntimeError("Missing SDR native library")
        result.update(numpy=numpy.__version__, pyserial=serial.VERSION, libraries=libraries)
    else:
        import common
        if not os.path.isfile(common.BACKEND) or common.digest(common.BACKEND) != common.BACKEND_SHA256:
            raise RuntimeError("Image FM backend hash mismatch")
        result.update(backend=common.BACKEND, backend_sha256=common.BACKEND_SHA256)
    print(json.dumps(result, sort_keys=True))

def wait_for_ready(process, read_fd, stopping):
    while not stopping[0]:
        if process.poll() is not None:
            raise RuntimeError("Radio daemon exited before readiness: " + str(process.returncode))
        readable, _, _ = select.select([read_fd], [], [], 0.2)
        if readable:
            value = os.read(read_fd, 64)
            if value == b"ready\n":
                return
            raise RuntimeError("Radio daemon readiness pipe closed without readiness")
    raise InterruptedError("Container stopped during daemon startup")

def wait_fm_watchdogs(state):
    # Preserve the existing watchdog's clock-cleanup opportunity before PID 1 exits.
    source = ROOT / "providers/pi-fm"
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    from common import identity, read
    while True:
        active = []
        for path in Path(state).glob("job-*/state.json"):
            record = read(str(path), {})
            owner = record.get("worker_identity")
            if owner and identity(owner.get("pid", -1)) == owner:
                active.append(owner["pid"])
        if not active:
            return
        for pid in active:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        time.sleep(0.1)

def serve(kind, home, state, runtime):
    home, state, runtime = Path(home), Path(state), Path(runtime)
    for path in (home, state):
        if not path.is_absolute() or not path.is_dir():
            raise ValueError("Existing absolute state/provider bind mounts are required")
    with (home / "manifest.json").open() as handle:
        manifest = rewrite_manifest(json.load(handle), kind, home)
    # No enroll/pair/initialize occurs here. Private material stays in the mounted home.
    if not (home / "config.json").is_file():
        raise ValueError("Existing enrolled provider config.json is required")
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    manifest_path = runtime / ("radio-" + kind + "-manifest.json")
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n")
    manifest_path.chmod(0o600)
    stopping = [False]
    children = []
    def stop(signum, frame):
        stopping[0] = True
        for child in children:
            if child.poll() is None:
                child.terminate()
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, stop)
    read_fd, write_fd = os.pipe()
    try:
        env = dict(os.environ, BIOMERTE_RADIO_READY_FD=str(write_fd))
        server = ROOT / "providers" / ("pi-" + kind) / "server.py"
        daemon = subprocess.Popen([sys.executable, str(server), "serve", str(home), str(state)],
                                  env=env, pass_fds=(write_fd,))
        children.append(daemon)
        os.close(write_fd)
        write_fd = None
        wait_for_ready(daemon, read_fd, stopping)
        provider = subprocess.Popen([sys.executable, "-m", "biomerte", "--home", str(home),
                                     "worker", str(manifest_path)], cwd=str(ROOT))
        children.append(provider)
        while not stopping[0] and all(child.poll() is None for child in children):
            time.sleep(0.2)
        failed = next((child.returncode for child in children if child.poll() is not None and child.returncode), 0)
        return 0 if stopping[0] else (failed or 1)
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)
        # Stop registration/request intake and daemon together. The FM watchdog observes parent loss.
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
        for child in children:
            child.wait()
        if kind == "fm":
            wait_fm_watchdogs(state)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("check", "serve"))
    parser.add_argument("--kind", choices=KINDS, default=os.environ.get("BIOMERTE_RADIO_KIND"))
    parser.add_argument("--home")
    parser.add_argument("--state")
    parser.add_argument("--runtime", default="/run/biomerte")
    args = parser.parse_args()
    if args.kind not in KINDS:
        parser.error("--kind is required")
    if args.action == "check":
        check(args.kind)
        return 0
    if not args.home or not args.state:
        parser.error("serve requires --home and --state")
    return serve(args.kind, args.home, args.state, args.runtime)

if __name__ == "__main__":
    raise SystemExit(main())
