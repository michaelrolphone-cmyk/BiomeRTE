#!/usr/bin/env python3
"""Declare, CAS-update and inspect generic Docker services."""
import argparse
import json
import os
import sys

def redact(value):
    if isinstance(value, dict):
        return {key: ("[redacted]" if key in ("grant_token", "secret", "token") else redact(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", required=True)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("status")
    declare = commands.add_parser("declare")
    declare.add_argument("manifest")
    declare.add_argument("--expected-revision")
    args = parser.parse_args()
    if not args.command:
        parser.error("Choose status or declare")
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, root)
    from biomerte.docker_services import CoordinatorClient, validate_manifest
    folder = os.path.abspath(os.path.expanduser(args.config_dir))
    with open(os.path.join(folder, "runner.json")) as source:
        config = json.load(source)
    client = CoordinatorClient(config["coordinator_url"], os.path.join(folder, "coordinator.key"))
    if args.command == "status":
        result = client.call("status", {})
    else:
        with open(args.manifest) as source:
            manifest = validate_manifest(json.load(source))
        body = {"manifest": manifest}
        if args.expected_revision:
            body["expected_revision"] = args.expected_revision
        result = client.call("configure", body)
    print(json.dumps(redact(result), sort_keys=True, indent=2))

if __name__ == "__main__":
    main()
