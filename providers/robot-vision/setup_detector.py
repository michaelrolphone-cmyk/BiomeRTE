#!/usr/bin/env python3
"""Install detector packages only into the dedicated vision environment."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
from pathlib import Path
import subprocess

from vision_config import DEFAULT_STATE, atomic_json

PACKAGES = [
    "torch==2.15.0", "torchvision==0.29.2", "av==18.1.0",
    "matplotlib==3.11.2", "Pillow==13.0.0", "PyYAML",
    "requests==2.34.2", "scipy", "psutil==7.2.2", "polars==2.1.0",
    "ultralytics-thop==2.2.5", "lap==0.5.14",
]
ULTRALYTICS = "ultralytics==8.4.208"


def install(state_dir, platform_profile="native-macos", cpu_index="https://download.pytorch.org/whl/cpu"):
    state = Path(state_dir).expanduser().resolve()
    env = state/"env"
    python = env/"bin/python"
    if not python.is_file():
        raise RuntimeError("Create the isolated SLAM environment before installing the detector.")
    constraints = state/"detector-constraints.txt"
    constraints.write_text("numpy==1.26.4\nopencv-python==0\nopencv-contrib-python==0\nopencv-python-headless==0\n")
    if platform_profile == "linux-cpu":
        if platform.system() != "Linux":
            raise RuntimeError("linux-cpu detector setup requires Linux.")
        torch_packages = [p for p in PACKAGES if p.startswith(("torch==", "torchvision=="))]
        subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                        "-c", str(constraints), "--index-url", cpu_index, *torch_packages], check=True)
        packages = [p for p in PACKAGES if p not in torch_packages]
    elif platform_profile == "native-macos":
        if platform.system() != "Darwin":
            raise RuntimeError("native-macos detector setup requires macOS.")
        packages = PACKAGES
    else:
        raise ValueError("Unknown detector platform profile.")
    subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                    "-c", str(constraints), *packages], check=True)
    subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                    "--no-deps", ULTRALYTICS], check=True)
    linkage = {"platform_profile": platform_profile}
    if platform_profile == "native-macos":
        shared = env/"lib/libomp.dylib"
        candidates = list((env/"lib").glob("python*/site-packages/torch/lib/libomp.dylib"))
        if len(candidates) != 1 or not shared.is_file():
            raise RuntimeError("Expected one torch OpenMP runtime and the environment's shared runtime")
        bundled = candidates[0]
        backup = bundled.with_name("libomp.dylib.biome-original")
        if not bundled.is_symlink():
            if backup.exists():
                raise RuntimeError("OpenMP backup exists; inspect the environment before replacing it")
            bundled.rename(backup)
            bundled.symlink_to(shared)
        elif bundled.resolve() != shared.resolve():
            raise RuntimeError("Unexpected torch OpenMP symlink")
        linkage.update(shared_openmp=str(shared), torch_openmp=str(bundled),
                       shared_sha256=hashlib.sha256(shared.read_bytes()).hexdigest(),
                       original_sha256=hashlib.sha256(backup.read_bytes()).hexdigest() if backup.exists() else None)
    child_env = os.environ.copy()
    child_env.pop("KMP_DUPLICATE_LIB_OK", None)
    for imports in ("import cv2; import torch", "import torch; import cv2"):
        subprocess.run([str(python), "-c", imports+"; import av, ultralytics; print('imports passed')"],
                       env=child_env, check=True)
    evidence = {
        **linkage,
        "import_orders_verified": True,
        "duplicate_runtime_override": False,
    }
    atomic_json(state/"runtime-linkage.json", evidence)
    with (state/"requirements-installed.txt").open("w") as output:
        subprocess.run([str(python), "-m", "pip", "freeze"], stdout=output, check=True)
    print(json.dumps(evidence))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--platform-profile", choices=("native-macos", "linux-cpu"),
                        default="native-macos")
    parser.add_argument("--cpu-index", default="https://download.pytorch.org/whl/cpu")
    args = parser.parse_args()
    install(args.state, args.platform_profile, args.cpu_index)
