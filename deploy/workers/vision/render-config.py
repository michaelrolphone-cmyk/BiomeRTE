"""Render a container provider manifest without changing native state or identity."""
import argparse
import json
from pathlib import Path

def render(native, host_io="/run/biome/host-io.json"):
    config = dict(native)
    for name in ("provider", "site", "resource", "state", "slots"):
        if name not in config:
            raise ValueError("native manifest missing " + name)
    config.update(repo="/app", python="/usr/local/bin/python",
                  adapter_path="/app/providers/robot-vision/biome_provider.py",
                  gateway_source="/app/gateway", host_io_config=host_io,
                  stream_local_router_url=None)
    config.pop("gateway_state", None)
    return config

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = render(json.loads(args.native_manifest.read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    args.output.chmod(0o600)

if __name__ == "__main__":
    main()
