#!/usr/bin/env python3
"""Install a user LaunchAgent to check the deployed controller and Rover after release changes."""

import hashlib
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile

LABEL = "org.biomerte.provider-qualification"
FILES = ("tests/__init__.py", "tests/qualify_live_provider_recovery.py", "tests/run_live_provider_recovery.py")


def main():
    if sys.platform != "darwin":
        raise SystemExit("The live Rover observer requires the deployed Mac bridge.")
    os.umask(0o077)
    root = Path(__file__).resolve().parents[1]
    home = Path.home()
    state = home / ".local/share/biomerte-provider-qualification"
    python = home / ".local/share/biome-robot-vision/env/bin/python"
    subprocess.run([str(python), "-c", "from aiohttp import ClientWSTimeout"], check=True)
    files = {name: (root / name).read_bytes() for name in FILES}
    checksum = hashlib.sha256()
    for name, body in files.items():
        checksum.update(name.encode() + b"\0" + hashlib.sha256(body).digest())
    release = state / "releases" / checksum.hexdigest()
    release.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not release.exists():
        with tempfile.TemporaryDirectory(dir=release.parent) as folder:
            staging = Path(folder) / "release"
            (staging / "tests").mkdir(parents=True)
            for name, body in files.items():
                (staging / name).write_bytes(body)
            staging.rename(release)
    spec = dict(Label=LABEL, ProgramArguments=[str(python), "-m", "tests.run_live_provider_recovery"],
                WorkingDirectory=str(release), RunAtLoad=True, StartInterval=300,
                ProcessType="Background", StandardOutPath="/dev/null", StandardErrorPath="/dev/null")
    path = home / "Library/LaunchAgents" / (LABEL + ".plist")
    domain = "gui/" + str(os.getuid())
    loaded = subprocess.run(["/bin/launchctl", "print", domain + "/" + LABEL], capture_output=True).returncode == 0
    content = plistlib.dumps(spec)
    if loaded and path.read_bytes() == content:
        print("Current production qualification agent is already loaded.")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
        temporary = Path(f.name)
    os.replace(temporary, path)
    if loaded:
        subprocess.run(["/bin/launchctl", "bootout", domain + "/" + LABEL], check=True)
    subprocess.run(["/bin/launchctl", "bootstrap", domain, str(path)], check=True)
    print("Production qualification agent loaded from", release.name)


if __name__ == "__main__":
    main()
