"""Run the complete CPU vision backend with an explicitly selected CPU configuration."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("backend", "qualify"))
    parser.add_argument("--state")
    parser.add_argument("--config")
    parser.add_argument("--model")
    args = parser.parse_args(argv)
    if args.mode == "qualify":
        if not args.model:
            parser.error("--model must name the mounted qualified detector weights")
        command = [sys.executable, str(ROOT/"deploy/workers/vision/qualify-cpu.py"), "--model", args.model]
    else:
        if not args.state or not args.config:
            parser.error("backend requires --state and --config")
        config = Path(args.config).resolve(strict=True)
        value = json.loads(config.read_text())
        if value.get("detector", {}).get("device") != "cpu":
            parser.error("This CPU image requires explicit detector.device=cpu; it never rewrites an MPS configuration.")
        command = [sys.executable, str(ROOT/"providers/robot-vision/vision_service.py"),
                   "--state", args.state, "--config", str(config)]
    os.execv(sys.executable, command)

if __name__ == "__main__":
    main()
