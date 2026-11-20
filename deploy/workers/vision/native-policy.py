"""Render the exact native vision query contract set for reviewed HostIO config."""
import argparse
import importlib.util
import json
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-python", required=True)
    parser.add_argument("--native-source", help="Absolute installed vision module directory; independent of route helper source")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location("vision_contract_policy",
            root/"providers/robot-vision/biome_provider.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    policy = {
        "operations": sorted({value["name"] for value in module.CONTRACTS.values()}),
        "mutations": sorted({module.CONTRACTS[key]["name"] for key in module.MUTATING_CONTRACTS}),
        "python": args.native_python
    }
    if args.native_source:
        if not Path(args.native_source).is_absolute():
            parser.error("--native-source must be absolute")
        policy["module_directory"] = args.native_source
    Path(args.output).write_text(json.dumps(policy, indent=2, sort_keys=True)+"\n")

if __name__ == "__main__":
    main()
