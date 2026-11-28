"""Run the SDR daemon and the enrolled capability worker under one lifecycle."""
import argparse
import ctypes.util
import importlib
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time

SOURCE = Path(__file__).resolve().parents[3] / "providers" / "pi-sdr"
SERVER = SOURCE / "server.py"
CONTRACTS = {"radio.sdr.receive:1", "radio.sdr.receive:2"}


def check():
    sys.path.insert(0, str(SOURCE))
    for name in ("server", "numpy", "serial"):
        importlib.import_module(name)
    if not all(ctypes.util.find_library(name) for name in ("rtlsdr", "usb-1.0")):
        raise RuntimeError("The SDR image is missing a native receiver library")


def serve(home, state):
    if not (home / "config.json").is_file() or not state.is_dir():
        raise ValueError("Existing provider configuration and receiver state are required")
    manifest = json.loads((home / "manifest.json").read_text())
    offers = manifest.get("offers", ())
    if not offers or any(offer.get("contract") not in CONTRACTS or not offer.get("resource") for offer in offers):
        raise ValueError("The manifest must belong to the enrolled SDR worker")
    for offer in offers:
        offer["argv"] = [sys.executable, str(SERVER), "call", str(home)]

    children = []
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)

    with tempfile.TemporaryDirectory(prefix="biomerte-sdr-") as runtime:
        manifest_path = Path(runtime) / "manifest.json"
        manifest_path.write_text(json.dumps(manifest))
        read_fd, write_fd = os.pipe()
        try:
            try:
                daemon = subprocess.Popen(
                    [sys.executable, str(SERVER), "serve", str(home), str(state)],
                    env=dict(os.environ, BIOMERTE_SDR_READY_FD=str(write_fd)),
                    pass_fds=(write_fd,),
                )
                children.append(daemon)
            finally:
                os.close(write_fd)
            while not stopping:
                if daemon.poll() is not None:
                    raise RuntimeError("SDR daemon exited before readiness")
                readable, _, _ = select.select([read_fd], [], [], 0.2)
                if readable:
                    if os.read(read_fd, 1) != b"1":
                        raise RuntimeError("SDR readiness pipe closed")
                    break
            if stopping:
                return 0
            children.append(subprocess.Popen([
                sys.executable, "-m", "biomerte", "--home", str(home),
                "worker", str(manifest_path),
            ]))
            while not stopping and all(child.poll() is None for child in children):
                time.sleep(0.2)
            return 0 if stopping else 1
        finally:
            os.close(read_fd)
            for child in reversed(children):
                if child.poll() is None:
                    child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "serve"))
    parser.add_argument("--home", type=Path)
    parser.add_argument("--state", type=Path)
    args = parser.parse_args()
    if args.mode == "check":
        check()
        return 0
    if not args.home or not args.state:
        parser.error("serve requires --home and --state")
    return serve(args.home.resolve(), args.state.resolve())


if __name__ == "__main__":
    sys.exit(main())
