"""Leased stream router with explicit network boundaries; no controller database."""
import argparse
import asyncio
import hmac
import json
import logging
import ssl
import re
import urllib.request

from biomerte.stream_router import Router, listener_tls
from biomerte.stream_inspection import add_remote
from biomerte.service_inspection import add_services
from biomerte.control_clock import ControlClock
from portable_common import read_json, stop_event, endpoint

class NoOwnerRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None

def open_owner(request, timeout):
    # These are fixed authenticated private endpoints. Neither redirects nor
    # environment proxies may forward the two existing Rover credentials.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoOwnerRedirect())
    return opener.open(request, timeout=timeout)

def inspect_owner(url, authority, secret, session):
    from rover_transport import derived
    body = json.dumps({"authority": authority, "session": session},
                      separators=(",", ":")).encode()
    request = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + derived(secret, "rover-site"),
        "X-Router-Inspect": derived(secret, "router-inspect"),
        "User-Agent": "BiomeRTE-Portable-Router/1"})
    with open_owner(request, timeout=5) as response:
        return json.load(response)

def add_owner_bridges(config, secrets, validators):
    for authority, settings in config.get("owner_bridge_controllers", {}).items():
        if (not isinstance(authority, str) or not re.fullmatch("[a-f0-9]{64}", authority) or
                authority in secrets or not isinstance(settings, dict) or
                not isinstance(settings.get("secret"), str) or len(settings["secret"]) < 32):
            raise ValueError("Invalid or duplicate owner bridge authority")
        url = endpoint(settings.get("inspect_url"),
                       local_http=settings.get("allow_local_http") is True,
                       path="/internal/stream/inspect")
        secret = settings["secret"]
        secrets[authority] = secret
        validators[authority] = lambda session, url=url, authority=authority, secret=secret: inspect_owner(
            url, authority, secret, session)

def configure(config):
    if not isinstance(config, dict):
        raise ValueError("Router configuration must be an object")
    if any(config.get(key) for key in (
            "controllers", "device_routes", "device_route_provisioning")):
        raise ValueError("Portable router requires remote inspection and host I/O; native paths are forbidden")
    if not config.get("remote_controllers") and not config.get("owner_bridge_controllers"):
        raise ValueError("Explicit remote controller validators are required")
    if type(config.get("port", 8751)) is not int or not 1 <= config.get("port", 8751) <= 65535:
        raise ValueError("Invalid router port")
    secrets, validators = {}, {}
    add_remote(config, secrets, validators)
    add_owner_bridges(config, secrets, validators)
    secrets, validators = add_services(config, secrets, validators)
    if config.get("transport") == "private_docker_http":
        if config.get("network_scope") != "private" or config.get("listen", "0.0.0.0") not in (
                "0.0.0.0", "127.0.0.1", "::", "::1"):
            raise ValueError("Plain Docker listener requires explicit private network scope")
        context = None
    else:
        context = listener_tls(config)
    return secrets, validators, context

async def serve(config):
    from websockets.asyncio.server import serve as websocket_server
    secrets, validators, context = configure(config)
    host_io = None
    if config.get("host_io_config"):
        from biomerte.host_io_client import HostIOClient
        host_io = HostIOClient.from_config(config["host_io_config"])
    host_ready = host_io is None
    async def refresh_host():
        nonlocal host_ready
        while True:
            try:
                await asyncio.to_thread(host_io.health)
                host_ready = True
            except Exception:
                host_ready = False
            await asyncio.sleep(5)
    host_task = asyncio.create_task(refresh_host()) if host_io else None
    clock = ControlClock() if config.get("control_ntp", True) else None
    clock_task = asyncio.create_task(clock.start()) if clock else None
    router = Router(secrets, validators, peer_status=config.get("peer_status", True),
                    control_clock=clock)
    stopped = stop_event()
    async def health(connection, request):
        if request.path == "/device-routes":
            supplied = request.headers.get("Authorization", "")
            if not any(hmac.compare_digest(supplied, "Bearer " + str(secret))
                       for secret in secrets.values()):
                return connection.respond(403, '{"error":"unauthorized"}')
            if host_io is None:
                return connection.respond(503, '{"error":"host_io_unconfigured"}')
            try:
                records = await asyncio.to_thread(host_io.list_routes)
            except Exception:
                return connection.respond(503, '{"error":"host_io_unavailable"}')
            return connection.respond(200, json.dumps({
                "schema": "biome.device-routes/1", "devices": records}))
        if request.path == "/health":
            clock_ready = clock is None or clock.ready
            ready = clock_ready and (host_ready or config.get("host_io_required") is False)
            response = connection.respond(200 if ready else 503, json.dumps({
                "state": "ready" if ready else "waiting_for_dependencies",
                "authorities": sorted(secrets), **router.health(),
                "transport": config.get("transport", "tls"),
                "control_clock": clock.status() if clock else None,
                "native_database_mounted": False,
                "host_io_configured": host_io is not None, "host_io_ready": host_ready}))
            del response.headers["Content-Type"]
            response.headers["Content-Type"] = "application/json"
            return response
    async def handle(socket):
        try:
            await router.attach(socket, socket.request.path,
                                socket.request.headers.get("Authorization", ""))
        except Exception as error:
            router.report("attachment", None, error)
            await socket.close(1008, "Stream rejected")
    try:
        async with websocket_server(handle, config.get("listen", "0.0.0.0"),
                config.get("port", 8751), ssl=context, process_request=health,
                max_size=None, max_queue=1, write_limit=65536):
            await stopped.wait()
    finally:
        await asyncio.gather(*(session.close("Router shutting down")
                               for session in list(router.sessions.values())), return_exceptions=True)
        if host_task:
            host_task.cancel()
            await asyncio.gather(host_task, return_exceptions=True)
        if clock_task:
            clock_task.cancel()
            await asyncio.gather(clock_task, return_exceptions=True)
        if clock:
            await clock.close()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/config/router.json")
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args()
    config = read_json(args.config)
    configure(config)
    if args.check_config:
        print(json.dumps({"state": "configuration_valid", "service": "stream-router",
                          "native_database_mounted": False}))
        return
    asyncio.run(serve(config))

if __name__ == "__main__":
    main()
