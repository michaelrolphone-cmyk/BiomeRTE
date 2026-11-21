"""Render actual motion worker manifests without changing native state or identity."""
import argparse
import json
from pathlib import Path

def render(native, rover_origin, token_file="/run/biome/rover.token",
           host_io="/run/biome/host-io.json"):
    provider = dict(native)
    for name in ("provider", "site", "resource", "state", "slots", "database"):
        if name not in provider:
            raise ValueError("native manifest missing " + name)
    provider.update(runtime="/app", worker_runtime="/app",
                    python="/usr/local/bin/python",
                    adapter_path="/app/providers/rover-motion/biome_provider.py",
                    gateway_source="/app/gateway")
    provider.pop("gateway_state", None)
    observer = {"database": provider["database"], "rover_origin": rover_origin,
                "rover_token_file": token_file, "host_io_config": host_io}
    return provider, observer

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-manifest", required=True, type=Path)
    parser.add_argument("--rover-origin", required=True,
                        help="Existing applied-control stream origin reachable from the container")
    parser.add_argument("--output-directory", required=True, type=Path)
    args = parser.parse_args()
    provider, observer = render(json.loads(args.native_manifest.read_text()), args.rover_origin)
    args.output_directory.mkdir(parents=True, exist_ok=True)
    for name, result in (("provider.json", provider), ("observer.json", observer)):
        path = args.output_directory/name
        path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        path.chmod(0o600)

if __name__ == "__main__":
    main()
