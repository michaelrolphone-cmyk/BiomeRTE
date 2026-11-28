"""Run the rover motion capability worker or its offline qualification."""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ADAPTER = ROOT / "providers" / "rover-motion" / "biome_provider.py"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    commands.add_parser("check")
    serve = commands.add_parser("serve")
    serve.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "check":
        os.execv(sys.executable, [
            sys.executable, "-m", "unittest", "discover", "-s", str(ROOT / "tests"),
            "-p", "test_motion_*.py",
        ])
    os.execv(sys.executable, [
        sys.executable, str(ADAPTER), "serve", "--state", str(args.state),
        "--packaged-runtime", str(ROOT),
    ])


if __name__ == "__main__":
    main()
