"""Configuration boundaries shared by portable service entrypoints."""
import asyncio
import ipaddress
import json
import signal
from pathlib import Path
from urllib.parse import urlsplit

def read_json(path):
    with open(path) as source:
        return json.load(source)

def private_host(host):
    if host in ("localhost", "host.docker.internal", "gateway.docker.internal"):
        return True
    try:
        address = ipaddress.ip_address(host or "")
        return address.is_loopback or address.is_private
    except ValueError:
        return False

def endpoint(value, *, local_http=False, path=None):
    if not isinstance(value, str):
        raise ValueError("An explicit endpoint URL is required")
    parsed = urlsplit(value)
    if (not parsed.hostname or parsed.username or parsed.password or
            parsed.query or parsed.fragment):
        raise ValueError("Endpoint must not contain credentials, query or fragment")
    if parsed.scheme != "https" and not (
            parsed.scheme == "http" and local_http and private_host(parsed.hostname)):
        raise ValueError("HTTPS or explicitly allowed private local HTTP is required")
    if path is not None and parsed.path != path:
        raise ValueError("Unexpected endpoint path")
    return value.rstrip("/")

def stop_event():
    event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, event.set)
    return event

def absolute_path(value, label):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError(label + " must be an absolute container path")
    return value
