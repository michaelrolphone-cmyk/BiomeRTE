"""Portable Rover frontend preserving signed calls and native clock boundaries.

This process is deliberately not registered as stateless with the failover
runner: pending-routes.sqlite contains recovery custody. The single-host Docker
mode requires its own durable state volume and a quiescent handoff. Distributed
activation remains blocked until a qualified state adapter exists.
"""
import argparse
import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
from uuid import UUID
from urllib.parse import urlsplit

from aiohttp import web
from biomerte.control_clock import ControlClock
from biomerte.host_io_client import HostIOClient
from biomerte.rover import Rover
from biomerte.service_routing import ServiceOwnerResolver
from biomerte.stream_router import decode
from camera_light import CameraLight
from rover_service import FreshRover, LocalAuthority, RoutedRemote, map_snapshot
from rover_transport import Outbound, derived, local_router
from site_bridge import SiteBridge
from portable_common import absolute_path, endpoint, read_json, stop_event

def configure(config):
    if not isinstance(config, dict):
        raise ValueError("Rover configuration must be an object")
    if config.get("listen_host", "127.0.0.1") not in ("127.0.0.1", "0.0.0.0"):
        raise ValueError("Rover listen_host must be loopback or the explicit container interface")
    if config.get("state_mode") != "single_host_durable":
        raise ValueError("Rover requires an explicit durable single-host state handoff")
    for name in ("controller_home", "identity_home", "state_dir", "controllers_file", "host_io_config"):
        absolute_path(config.get(name), name)
    if config["state_dir"] in (config["identity_home"], config["controller_home"]):
        raise ValueError("Rover recovery state must be separate from read-only identities")
    endpoint(config.get("controller_api"), local_http=config.get("allow_private_controller_http") is True,
             path="/v1/call")
    endpoint(config.get("gateway"))
    endpoint(config.get("public_stream_gateway"))
    local_router(config.get("router_url"), config.get("private_router_hosts", ()))
    resources = config.get("resources")
    if (not isinstance(resources, dict) or set(resources) != {
            "robot.vision.stream:1", "device.outputs.stream:1"} or
            any(not isinstance(value, str) or not value for value in resources.values())):
        raise ValueError("Explicit vision and control resources are required")
    vision = config.get("vision_clock", {})
    if (vision.get("resource") != resources["robot.vision.stream:1"] or
            not isinstance(vision.get("host_id"), str) or not vision["host_id"]):
        raise ValueError("Vision processing clock must bind the registered source host and resource")
    light = config.get("camera_light", {})
    if not isinstance(light.get("device_id"), str) or not light["device_id"]:
        raise ValueError("Camera light requires a registered device identity")
    # Only the host adapter resolves this configured origin; no frozen address fallback.
    light_url = urlsplit(light.get("url", ""))
    if (light_url.scheme not in ("http", "https") or not light_url.hostname or
            light_url.username or light_url.password or light_url.query or light_url.fragment or
            light_url.path != "/api/light" or (light_url.scheme == "http" and
            config.get("allow_private_device_http") is not True)):
        raise ValueError("Camera light requires its fixed credential-free device API origin")
    return config

class PortableRover(FreshRover):
    def __init__(self, *args, host_io, vision_clock, **kwargs):
        super().__init__(*args, **kwargs)
        self.host_io = host_io
        self.vision_clock_config = dict(vision_clock)
        self.clock_bounds = None
        self.stream_clock_id = None
        self.clock_task = None
        self.clock_rejections = 0

    async def refresh_host_clock(self):
        try:
            bounds = await asyncio.to_thread(self.host_io.clock)
            if bounds.host_id != self.vision_clock_config["host_id"] or not bounds.valid:
                raise ValueError("Vision host clock identity is not current")
            self.clock_bounds = bounds
        except Exception:
            self.clock_bounds = None

    async def maintain_host_clock(self):
        while True:
            await self.refresh_host_clock()
            await asyncio.sleep(3)

    async def start(self):
        await self.refresh_host_clock()
        self.clock_task = asyncio.create_task(self.maintain_host_clock())
        await super().start()

    async def close(self):
        try:
            await super().close()
        finally:
            if self.clock_task:
                self.clock_task.cancel()
                await asyncio.gather(self.clock_task, return_exceptions=True)

    async def lane(self, descriptor, lane):
        if lane == "vision":
            bounds = self.clock_bounds
            if (bounds is None or not bounds.valid or
                    descriptor.get("resource") != self.vision_clock_config["resource"]):
                raise ConnectionError("Vision source clock is not verified")
            # A changed host boot cannot relabel frames from an existing stream.
            # A fresh attachment under the configured native source is required.
            self.stream_clock_id = bounds.clock_id
        return await super().lane(descriptor, lane)

    def broadcast(self, lane, message):
        if lane == "vision":
            metadata, unused = decode(message)
            if metadata.get("type") not in ("sensor_manifest", "world_snapshot"):
                bounds = self.clock_bounds
                processed = metadata.get("processed_monotonic")
                source_clock_id = metadata.get("processing_clock_id")
                if source_clock_id is None:
                    if self.vision_clock_config.get("allow_legacy_host_monotonic") is not True:
                        self.clock_rejections += 1
                        return
                    source_clock_id = self.stream_clock_id
                try:
                    if bounds is None or source_clock_id is None:
                        raise ValueError("Unknown vision clock domain")
                    low, high = bounds.age(processed, source_clock_id=source_clock_id)
                    if not 0 <= low <= high <= .5:
                        raise ValueError("Vision processing age exceeds its original freshness bound")
                except (ValueError, TypeError):
                    self.clock_rejections += 1
                    return
        # Call the common core directly; FreshRover's original host-only
        # monotonic comparison is intentionally replaced by the bounded bridge.
        Rover.broadcast(self, lane, message)

def survey_adapter(host_io):
    async def zero(body):
        try:
            if set(body) != {"type", "request_id", "world_id"} or body["type"] != "survey_zero":
                raise ValueError()
            UUID(body["world_id"])
            UUID(body["request_id"])
        except (ValueError, KeyError, TypeError, AttributeError):
            return web.json_response({"error": {"message": "Invalid survey zero request"}}, status=400)
        try:
            result = await asyncio.to_thread(host_io.survey_zero, body["world_id"], body["request_id"])
        except (OSError, ValueError, TimeoutError):
            return web.json_response({"error": {"message":
                "Survey estimator did not acknowledge the reset"}}, status=503)
        if result.get("ok") is not True:
            return web.json_response({"error": {"message":
                result.get("error", "Survey zero unavailable")}}, status=409)
        return web.json_response(result)
    return zero

async def run(config):
    configure(config)
    if os.environ.get("BIOME_SERVICE_ID") or os.environ.get("BIOME_SERVICE_PHASE"):
        raise RuntimeError("Rover failover admission requires a qualified recovery-state adapter; stateless declaration is forbidden")
    home = Path(config["identity_home"])
    state = Path(config["state_dir"])
    state.mkdir(parents=True, exist_ok=True)
    os.environ["ROVER_CONTROLLERS_FILE"] = config["controllers_file"]
    host_io = HostIOClient.from_config(config["host_io_config"])
    app = LocalAuthority(config["controller_home"], home, endpoint=config["controller_api"])
    secret = app.config["stream_router_secret"]
    resolver = ServiceOwnerResolver(config["public_stream_gateway"],
        declarations=lambda: read_json(home / "node.json").get("service_authority_declarations", []))
    remote = RoutedRemote(app, home, resolver=resolver, state_home=state, gateway_url=config["gateway"])
    clock = await ControlClock().start()
    rover = PortableRover(remote, config["resources"], task=config.get("task", "rover-site-server"),
                          clock=clock, host_io=host_io, vision_clock=config["vision_clock"])
    rover.local_router = local_router(config.get("router_url"), config.get("private_router_hosts", ()))
    rover.require_provider_ready = bool(rover.local_router)
    async def resolve_light(url):
        resolved = await asyncio.to_thread(host_io.origin, url, config["camera_light"]["device_id"])
        return endpoint(resolved, local_http=config.get("allow_private_device_http") is True)
    light = CameraLight(config["camera_light"]["url"], resolve_url=resolve_light)
    bridge = SiteBridge(rover, derived(secret, "rover-site"), config["channels"],
        snapshot=map_snapshot(rover), intent_ttl_ms=config.get("intent_ttl_ms", 500),
        camera_light=light, survey_zero=survey_adapter(host_io))
    application = bridge.app()
    @web.middleware
    async def health(request, handler):
        if request.path == "/health" and request.method == "GET":
            bounds = rover.clock_bounds
            ready = clock.ready and bounds is not None and bounds.valid
            return web.json_response({"state": "ready" if ready else "waiting_for_clocks",
                "control_ready": rover.control_ready, "vision_clock_ready": bounds is not None and bounds.valid,
                "vision_clock_rejections": rover.clock_rejections,
                "state_mode": "single_host_durable", "automatic_failover_qualified": False},
                status=200 if ready else 503)
        return await handler(request)
    application.middlewares.insert(0, health)
    async def inspect_stream(request):
        import hmac
        import uuid
        if not hmac.compare_digest(request.headers.get("X-Router-Inspect", ""), derived(secret, "router-inspect")):
            raise web.HTTPUnauthorized()
        value = await request.json()
        if (not isinstance(value, dict) or set(value) != {"authority", "session"} or
                value["authority"] != app.node or not isinstance(value["session"], str) or
                len(value["session"]) != 32 or any(c not in "0123456789abcdef" for c in value["session"])):
            raise web.HTTPBadRequest()
        try:
            grant = await asyncio.to_thread(app.call, "stream.inspect",
                                           {"session": value["session"]}, uuid.uuid4().hex, realm="site")
            return web.json_response(grant)
        except Exception:
            raise web.HTTPConflict(text="Stream authority unavailable")
    application.router.add_post("/internal/stream/inspect", inspect_stream)
    runner = web.AppRunner(application)
    stopped = stop_event()
    transport = waiter = None
    try:
        await runner.setup()
        # Native defaults retain loopback; the container explicitly binds its interface
        # so Docker can preserve the authenticated host-loopback observer endpoint.
        port = config.get("port", 8093)
        await web.TCPSite(runner, config.get("listen_host", "127.0.0.1"), port).start()
        transport = asyncio.create_task(Outbound(config["public_stream_gateway"],
            "http://127.0.0.1:" + str(port), secret, rover.local_router,
            allowed_private_router_hosts=config.get("private_router_hosts", ())).run())
        waiter = asyncio.create_task(stopped.wait())
        done, unused = await asyncio.wait((transport, waiter), return_when=asyncio.FIRST_COMPLETED)
        if transport in done:
            await transport
    finally:
        for task in (transport, waiter):
            if task:
                task.cancel()
        await asyncio.gather(*(task for task in (transport, waiter) if task), return_exceptions=True)
        await runner.cleanup()
        await clock.close()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/config/rover.json")
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args()
    config = configure(read_json(args.config))
    if args.check_config:
        print(json.dumps({"state": "configuration_valid", "service": "rover",
                          "automatic_failover_qualified": False}))
        return
    asyncio.run(run(config))

if __name__ == "__main__":
    main()
