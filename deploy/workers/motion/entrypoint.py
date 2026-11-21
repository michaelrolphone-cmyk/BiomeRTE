"""Run the actual motion observer or capability implementation from its image."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("observer", "provider", "qualify"))
    parser.add_argument("--config")
    args = parser.parse_args(argv)
    if args.mode == "qualify":
        os.chdir(ROOT)
        os.execv(sys.executable, [sys.executable, "-m", "unittest", "discover",
                                 "-s", str(ROOT/"tests"), "-p", "test_motion_*.py", "-v"])
    if not args.config:
        parser.error("--config is required for a running worker")
    path = Path(args.config).resolve(strict=True)
    config = json.loads(path.read_text())
    if args.mode == "observer":
        os.environ["BIOME_MOTION_OBSERVER_CONFIG"] = str(path)
        command = [sys.executable, str(ROOT/"providers/rover-motion/observer.py")]
    else:
        os.environ["BIOME_MOTION_MANIFEST"] = str(path)
        command = [sys.executable, str(ROOT/"providers/rover-motion/biome_provider.py"),
                   "serve", "--state", config["state"]]
    os.execv(sys.executable, command)

if __name__ == "__main__":
    main()
