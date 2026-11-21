"""Run the packaged vision capability implementation or full compute backend."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("provider", "backend", "qualify"))
    parser.add_argument("--config")
    parser.add_argument("--state")
    args = parser.parse_args(argv)
    if args.mode == "qualify":
        os.execv(sys.executable, [sys.executable, str(ROOT/"deploy/workers/vision/qualify.py")])
    if not args.config:
        parser.error("--config is required for a running worker")
    path = Path(args.config).resolve(strict=True)
    if args.mode == "provider":
        config = json.loads(path.read_text())
        os.environ["BIOME_VISION_MANIFEST"] = str(path)
        command = [sys.executable, str(ROOT/"providers/robot-vision/biome_provider.py"),
                   "serve", "--state", config["state"]]
    else:
        if not args.state:
            parser.error("--state is required for a compute backend")
        # No automatic MPS-to-CPU rewrite. The selected backend configuration
        # must state the intended device and model/dependency locations.
        command = [sys.executable, str(ROOT/"providers/robot-vision/vision_service.py"),
                   "--state", args.state, "--config", str(path)]
    os.execv(sys.executable, command)

if __name__ == "__main__":
    main()
