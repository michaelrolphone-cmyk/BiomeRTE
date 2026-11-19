"""Health probe for a configured portable service, without dumping private config."""
import argparse
import json
from urllib.request import urlopen

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8751/health")
    args = parser.parse_args()
    with urlopen(args.url, timeout=3) as response:
        value = json.load(response)
    if value.get("state") != "ready":
        raise SystemExit("service not ready")

if __name__ == "__main__":
    main()
