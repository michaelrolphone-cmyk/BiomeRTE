"""Isolated transport and custody checks for the captured portable source closure."""
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
PORTABLE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PORTABLE), str(PORTABLE / "source")]
# The Docker build copies this one independently maintained stdlib client.
import biomerte
spec = importlib.util.spec_from_file_location("biomerte.host_io_client", ROOT / "biomerte/host_io_client.py")
host_client = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = host_client
spec.loader.exec_module(host_client)
from biomerte.host_io_client import ClockBounds
from biomerte.rover import Rover
from biomerte.stream_router import Session, frame
from biomerte.store import Fault
import router
import rover
from rover_routes import ControllerRoutes
from rover_service import LocalAuthority
from rover_transport import local_router, Outbound
from camera_light import CameraLight
from site_bridge import SiteBridge

def router_config():
    return {"transport": "private_docker_http", "network_scope": "private",
            "listen": "0.0.0.0", "control_ntp": False,
            "remote_controllers": {"a" * 64: {"secret": "s" * 64,
                "inspect_url": "https://example.invalid/internal/stream/inspect"}}}

class Configuration(unittest.TestCase):
    def test_no_native_database_paths_or_implicit_public_listener(self):
        config = router_config()
        router.configure(config)
        for field in ("controllers", "device_routes", "device_route_provisioning"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                router.configure(dict(config, **{field: "/native/path"}))
        with self.assertRaises(ValueError):
            router.configure({**config, "network_scope": "unspecified"})

    def test_existing_owner_inspection_credentials_and_read_only_body(self):
        config = {"transport": "private_docker_http", "network_scope": "private",
                  "owner_bridge_controllers": {"a" * 64: {"secret": "s" * 64,
                      "inspect_url": "http://host.docker.internal:8093/internal/stream/inspect",
                      "allow_local_http": True}}}
        secrets, validators, unused = router.configure(config)
        reply = mock.MagicMock()
        reply.__enter__.return_value = reply
        reply.read.return_value = b'{"state":"open"}'
        with mock.patch("router.open_owner", return_value=reply) as opened:
            self.assertEqual(validators["a" * 64]("b" * 32)["state"], "open")
        request = opened.call_args.args[0]
        self.assertEqual(json.loads(request.data), {"authority": "a" * 64, "session": "b" * 32})
        from rover_transport import derived
        self.assertEqual(request.get_header("Authorization"), "Bearer " + derived("s" * 64, "rover-site"))
        self.assertEqual(request.get_header("X-router-inspect"), derived("s" * 64, "router-inspect"))
        with self.assertRaises(ValueError):
            router.configure({**config, "remote_controllers": {
                "a" * 64: {"secret": "s" * 64, "inspect_url": "https://example.invalid/stream/inspect"}}})

    def test_private_preparation_preserves_remote_authorities_without_native_paths(self):
        from prepare_router import prepared_config
        native = {"controllers": ["/native/controller"], "device_routes": "/native/routes.sqlite",
                  "remote_controllers": {"b" * 64: {"secret": "q" * 64,
                      "inspect_url": "https://example.invalid/stream/inspect"}},
                  "service_authority_secret": "r" * 64,
                  "service_inspect_url": "https://example.invalid/stream/inspect"}
        result = prepared_config(native, [{"node": "a" * 64, "stream_router_secret": "s" * 64}],
            owner_inspect_url="http://host.docker.internal:8093/internal/stream/inspect",
            allow_owner_local_http=True)
        self.assertEqual(result["remote_controllers"], native["remote_controllers"])
        self.assertEqual(result["service_authority_secret"], native["service_authority_secret"])
        self.assertEqual(set(result["owner_bridge_controllers"]), {"a" * 64})
        self.assertNotIn("controllers", result)
        self.assertNotIn("device_routes", result)

    def test_source_capture_bytes_and_runtime_import_boundary(self):
        value = json.loads((PORTABLE / "source-provenance.json").read_text())
        self.assertEqual(len(value["files"]), 16)
        for item in value["files"]:
            digest = hashlib.sha256((PORTABLE / "source" / item["path"]).read_bytes()).hexdigest()
            self.assertEqual(digest, item["packaged_sha256"], item["path"])
        self.assertNotIn("biomerte.app", sys.modules)
        self.assertNotIn("biomerte.runtime", sys.modules)

    def test_private_router_names_require_explicit_binding(self):
        self.assertEqual(local_router("ws://127.0.0.1:8751"), "ws://127.0.0.1:8751")
        with self.assertRaises(ValueError):
            local_router("ws://stream-router:8751")
        self.assertEqual(local_router("ws://stream-router:8751", ["stream-router"]), "ws://stream-router:8751")
        for invalid in ("ws://user:secret@stream-router:8751", "ws://stream-router:8751/path",
                        "ws://stream-router:8751?token=secret"):
            with self.assertRaises(ValueError):
                local_router(invalid, ["stream-router"])
        outbound = Outbound("https://example.invalid", "http://127.0.0.1:8093",
                            "s" * 64, "ws://stream-router:8751", ["stream-router"])
        self.assertEqual(outbound.native_router, "ws://stream-router:8751")

    def test_controller_api_preserves_signed_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "controller").mkdir()
            (root / "caller").mkdir()
            (root / "controller/node.json").write_text(json.dumps({"site": "test-site", "node": "a" * 64}))
            (root / "caller/node.json").write_text(json.dumps({"node": "b" * 64}))
            authority = LocalAuthority(root / "controller", root / "caller",
                                       endpoint="http://host.docker.internal:8740/v1/call")
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = b'{"result":{"state":"open"}}'
            with mock.patch("rover_service.sign", return_value="fixture-signature"), mock.patch(
                    "rover_service.urllib.request.urlopen", return_value=response) as opened:
                result = authority.call("stream.inspect", {"session": "c" * 32}, "request-id")
            request = opened.call_args.args[0]
            body = json.loads(request.data)
            self.assertEqual(request.full_url, "http://host.docker.internal:8740/v1/call")
            self.assertEqual(body["controller"], "a" * 64)
            self.assertEqual(body["auth"]["actor"], "b" * 64)
            self.assertEqual(body["id"], "request-id")
            self.assertEqual(result["state"], "open")

class Custody(unittest.TestCase):
    def test_lost_submission_retains_origin_across_restart_and_new_placement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "controllers.json"
            inventory.write_text(json.dumps({"authorities": ["a" * 64, "b" * 64]}))
            attempts = []
            def lost(owner, operation, arguments):
                attempts.append(owner)
                raise OSError("fixture lost response")
            with mock.patch.dict(os.environ, {"ROVER_CONTROLLERS_FILE": str(inventory)}):
                first = ControllerRoutes("a" * 64, root, lost)
                first.resolve_new = lambda *unused: "a" * 64
                arguments = {"contract": "read:1", "resource": "fixture", "operation": "same-command"}
                with self.assertRaises(OSError):
                    first.call("cap.submit", arguments)
                second = ControllerRoutes("a" * 64, root, lost)
                second.resolve_new = lambda *unused: "b" * 64
                with self.assertRaises(OSError):
                    second.call("cap.submit", arguments)
                self.assertEqual(second.operations.owner("same-command"), "a" * 64)
                self.assertEqual(attempts, ["a" * 64, "a" * 64])

class Freshness(unittest.TestCase):
    def make_rover(self, legacy=True):
        value = rover.PortableRover(None, {"robot.vision.stream:1": "vision.fixture"},
            host_io=None, vision_clock={"resource": "vision.fixture", "host_id": "registered-host",
                                        "allow_legacy_host_monotonic": legacy})
        value.clock_bounds = ClockBounds("boot-one", 999.99, 1000.01, 1000.,
            clock=lambda: 1000.2, host_id="registered-host")
        value.stream_clock_id = "boot-one"
        return value

    def test_cross_epoch_freshness_preserves_original_frame_bytes(self):
        value = self.make_rover()
        message = frame({"type": "frame", "processed_monotonic": 2000.1}, b"jpeg")
        with mock.patch.object(Rover, "broadcast") as sent:
            value.broadcast("vision", message)
            sent.assert_called_once_with(value, "vision", message)
        self.assertEqual(value.clock_rejections, 0)

    def test_old_future_unknown_and_previous_boot_frames_never_refresh(self):
        value = self.make_rover()
        with mock.patch.object(Rover, "broadcast") as sent:
            for timestamp in (1999., 2001., None, float("nan")):
                value.broadcast("vision", json.dumps({"type": "frame", "processed_monotonic": timestamp}))
            value.stream_clock_id = "previous-boot"
            value.broadcast("vision", json.dumps({"type": "frame", "processed_monotonic": 2000.1}))
            self.assertEqual(value.clock_rejections, 5)
            sent.assert_not_called()

    def test_legacy_clock_requires_explicit_source_assertion(self):
        value = self.make_rover(legacy=False)
        with mock.patch.object(Rover, "broadcast") as sent:
            value.broadcast("vision", json.dumps({"type": "frame", "processed_monotonic": 2000.1}))
            sent.assert_not_called()
            value.broadcast("vision", json.dumps({"type": "frame", "processed_monotonic": 2000.1,
                                                  "processing_clock_id": "boot-one"}))
            self.assertEqual(sent.call_count, 1)

    def test_control_core_retains_expiry_and_epoch_fences(self):
        grant = {"authority": "a", "generation": 3, "epoch": "e", "lanes": ["control"],
                 "direction": "server_to_provider", "limits": {"max_message_bytes": 4096}}
        session = Session(grant)
        message = {"type": "desired", "authority": "a", "generation": 3, "epoch": "e",
                   "sequence": 1, "outputs": {"mask": 0}, "issued_at": 100., "expires_at": 100.5}
        session.check("server", "control", json.dumps(message), now=100.1)
        with self.assertRaises(Fault):
            session.check("server", "control", json.dumps({**message, "sequence": 2}), now=100.6)
        with self.assertRaises(Fault):
            session.check("server", "control", json.dumps({**message, "sequence": 2, "epoch": "old"}), now=100.1)

class Adapters(unittest.IsolatedAsyncioTestCase):
    async def test_owner_credentials_never_follow_redirects_or_environment_proxies(self):
        from aiohttp import web
        from urllib.error import HTTPError
        hits = []
        app = web.Application()
        async def redirect(request):
            raise web.HTTPFound("/credential-target")
        async def target(request):
            hits.append(dict(request.headers))
            return web.json_response({"state": "open"})
        app.router.add_post("/internal/stream/inspect", redirect)
        app.router.add_route("*", "/credential-target", target)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            with mock.patch.dict(os.environ, {"http_proxy": "http://proxy.invalid:1", "no_proxy": ""}):
                with self.assertRaises(HTTPError) as caught:
                    await asyncio.to_thread(router.inspect_owner,
                        "http://127.0.0.1:" + str(port) + "/internal/stream/inspect",
                        "a" * 64, "s" * 64, "b" * 32)
            self.assertEqual(caught.exception.code, 302)
            self.assertEqual(hits, [])
        finally:
            await runner.cleanup()

    async def test_survey_ids_and_failure_status_are_preserved_without_replay(self):
        host = mock.Mock()
        host.survey_zero.return_value = {"ok": True}
        body = {"type": "survey_zero", "world_id": "11111111-1111-1111-1111-111111111111",
                "request_id": "22222222-2222-2222-2222-222222222222"}
        action = rover.survey_adapter(host)
        self.assertEqual((await action(body)).status, 200)
        host.survey_zero.assert_called_once_with(body["world_id"], body["request_id"])
        host.survey_zero.side_effect = OSError("fixture lost acknowledgement")
        self.assertEqual((await action(body)).status, 503)
        self.assertEqual(host.survey_zero.call_count, 2)
        self.assertEqual((await action({**body, "world_id": "invalid"})).status, 400)
        self.assertEqual(host.survey_zero.call_count, 2)

    async def test_native_survey_zero_uses_configured_estimator_and_matches_receipts(self):
        from aiohttp import web
        from survey_zero import zero_survey

        body = {"type": "survey_zero", "world_id": "11111111-1111-1111-1111-111111111111",
                "request_id": "22222222-2222-2222-2222-222222222222"}
        credential = "test-survey-estimator-credential-123456789"
        requests = []
        replies = iter([
            (200, {"ok": True, "world_id": body["world_id"], "reset_id": body["request_id"]}),
            (400, {"ok": False, "error": "survey_request_rejected"}),
            (200, {"ok": True, "world_id": body["world_id"], "reset_id": "unrelated"}),
        ])

        async def estimator(request):
            requests.append((request.headers.get("Authorization"), await request.json()))
            status, reply = next(replies)
            return web.json_response(reply, status=status)

        app = web.Application()
        app.router.add_post("/v1/survey-zero", estimator)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            with tempfile.TemporaryDirectory() as name:
                home = Path(name)
                token = home / "estimator-credential"
                token.write_text(credential)
                binding = home / ".local/share/biome-rover-server/survey-estimator.json"
                binding.parent.mkdir(parents=True)
                binding.write_text(json.dumps({"url": f"http://127.0.0.1:{port}",
                    "allow_local_http": True, "token_file": str(token)}))
                with mock.patch("survey_zero.Path.home", return_value=home):
                    self.assertEqual((await zero_survey(body)).status, 200)
                    rejected = await zero_survey(body)
                    self.assertEqual(rejected.status, 409)
                    self.assertIn("Survey estimator rejected", rejected.text)
                    self.assertEqual((await zero_survey(body)).status, 503)
            expected = ("Bearer " + credential, {**body, "type": "request"})
            self.assertEqual(requests, [expected, expected, expected])
        finally:
            await runner.cleanup()

    async def test_camera_reads_resolve_current_registered_route_each_time(self):
        from aiohttp import web
        servers = []
        urls = []
        for on in (False, True):
            app = web.Application()
            async def reply(request, on=on):
                return web.json_response({"ready": True, "command_state_known": True,
                                          "on": on, "brightness": 255, "rgb": [255, 255, 255]})
            app.router.add_get("/api/light", reply)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            servers.append(runner)
            urls.append("http://127.0.0.1:" + str(port) + "/api/light")
        calls = []
        async def resolve(origin):
            calls.append(origin)
            return urls[len(calls) - 1]
        light = CameraLight("http://registered-device.invalid/api/light", resolve_url=resolve)
        try:
            self.assertFalse((await light.request({}))["led"]["on"])
            self.assertTrue((await light.request({}))["led"]["on"])
            self.assertEqual(len(calls), 2)
        finally:
            await light.close()
            await asyncio.gather(*(server.cleanup() for server in servers))

    async def test_all_current_rover_ui_routes_remain_present(self):
        fake_rover = self.make_fixture_rover()
        bridge = SiteBridge(fake_rover, "s" * 64, {"forward": 1, "reverse": 2, "left": 3, "right": 4})
        application = bridge.app()
        paths = {route.resource.canonical for route in application.router.routes()}
        self.assertTrue({"/api/state", "/api/controls", "/api/led", "/api/video",
                         "/api/map/snapshot", "/api/live/{lane}"}.issubset(paths))
        await bridge.camera_light.close()

    def make_fixture_rover(self):
        return Rover(None, {"robot.vision.stream:1": "vision.fixture"})

if __name__ == "__main__":
    unittest.main(verbosity=2)
