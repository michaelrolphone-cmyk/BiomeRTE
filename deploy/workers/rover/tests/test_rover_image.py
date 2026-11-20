"""Behavioral image checks for the actual Rover application; no live devices."""
import asyncio
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

if (Path("/app") / "rover.py").is_file():
    APP = Path("/app")
    sys.path.insert(0, str(APP))
else:
    ROOT = Path(__file__).resolve().parents[4]
    APP = ROOT / "deploy/portable-services"
    sys.path[:0] = [str(APP), str(APP / "source")]
    import biomerte
    spec = importlib.util.spec_from_file_location("biomerte.host_io_client", ROOT / "biomerte/host_io_client.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from biomerte.host_io_client import ClockBounds
from biomerte.rover import Rover
from biomerte.stream_router import Session, frame
from biomerte.store import Fault
import rover
from rover_routes import ControllerRoutes
from rover_service import LocalAuthority
from site_bridge import SiteBridge

class SourceAndCustody(unittest.TestCase):
    def test_exact_image_source_closure(self):
        manifest = APP / "source-provenance.json" if APP == Path("/app") else ROOT / "deploy/workers/rover/source-provenance.json"
        value = json.loads(manifest.read_text())
        for item in value["image_files"]:
            path = APP / item["image_path"] if APP == Path("/app") else ROOT / item["repository_path"]
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), item["sha256"], item["image_path"])
        self.assertNotIn("biomerte.app", sys.modules)

    def test_signed_controller_destination_and_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "controller").mkdir(); (root / "caller").mkdir()
            (root / "controller/node.json").write_text(json.dumps({"site":"fixture", "node":"a"*64}))
            (root / "caller/node.json").write_text(json.dumps({"node":"b"*64}))
            client = LocalAuthority(root/"controller", root/"caller", endpoint="http://host.docker.internal:8740/v1/call")
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = b'{"result":{"state":"open"}}'
            with mock.patch("rover_service.sign", return_value="fixture"), mock.patch("rover_service.urllib.request.urlopen", return_value=response) as opened:
                self.assertEqual(client.call("stream.inspect", {"session":"c"*32}, "fixture-id"), {"state":"open"})
            request = opened.call_args.args[0]
            body = json.loads(request.data)
            self.assertEqual(request.full_url, "http://host.docker.internal:8740/v1/call")
            self.assertEqual(body["controller"], "a"*64)
            self.assertEqual(body["auth"]["actor"], "b"*64)
            self.assertEqual(body["id"], "fixture-id")

    def test_uncertain_submission_keeps_original_authority_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); inventory=root/"controllers.json"
            inventory.write_text(json.dumps({"authorities":["a"*64,"b"*64]}))
            attempts=[]
            def lost(owner, operation, arguments):
                attempts.append(owner)
                raise OSError("fixture lost acknowledgement")
            with mock.patch.dict(os.environ, {"ROVER_CONTROLLERS_FILE":str(inventory)}):
                first=ControllerRoutes("a"*64,root,lost); first.resolve_new=lambda *args:"a"*64
                args={"contract":"read:1","resource":"fixture","operation":"same-command"}
                with self.assertRaises(OSError): first.call("cap.submit",args)
                second=ControllerRoutes("a"*64,root,lost); second.resolve_new=lambda *args:"b"*64
                with self.assertRaises(OSError): second.call("cap.submit",args)
                self.assertEqual(attempts,["a"*64,"a"*64])
                self.assertEqual(second.operations.owner("same-command"),"a"*64)

    def test_original_stream_epoch_and_expiry_fences(self):
        session=Session({"authority":"a","generation":3,"epoch":"e","lanes":["control"],
                         "direction":"server_to_provider","limits":{"max_message_bytes":4096}})
        msg={"type":"desired","authority":"a","generation":3,"epoch":"e","sequence":1,
             "outputs":{"mask":0},"issued_at":100.,"expires_at":100.5}
        session.check("server","control",json.dumps(msg),now=100.1)
        with self.assertRaises(Fault): session.check("server","control",json.dumps({**msg,"sequence":2}),now=100.6)
        with self.assertRaises(Fault): session.check("server","control",json.dumps({**msg,"sequence":2,"epoch":"old"}),now=100.1)

class ClockFence(unittest.TestCase):
    def value(self):
        value=rover.PortableRover(None,{"robot.vision.stream:1":"vision.fixture"},host_io=None,
            vision_clock={"resource":"vision.fixture","host_id":"registered-host","allow_legacy_host_monotonic":True})
        value.clock_bounds=ClockBounds("boot-one",999.99,1000.01,1000.,clock=lambda:1000.2,host_id="registered-host")
        value.stream_clock_id="boot-one"
        return value

    def test_fresh_cross_epoch_frame_preserves_bytes(self):
        value=self.value(); message=frame({"type":"frame","processed_monotonic":2000.1},b"fixture-jpeg")
        with mock.patch.object(Rover,"broadcast") as sent:
            value.broadcast("vision",message)
            sent.assert_called_once_with(value,"vision",message)

    def test_stale_future_or_previous_boot_frames_rejected(self):
        value=self.value()
        with mock.patch.object(Rover,"broadcast") as sent:
            for stamp in (1999.,2001.,None,float("nan")):
                value.broadcast("vision",json.dumps({"type":"frame","processed_monotonic":stamp}))
            value.stream_clock_id="previous-boot"
            value.broadcast("vision",json.dumps({"type":"frame","processed_monotonic":2000.1}))
            self.assertEqual(value.clock_rejections,5)
            sent.assert_not_called()

class FixtureRover(Rover):
    def __init__(self):
        super().__init__(None,{"robot.vision.stream:1":"vision.fixture"})
        self.subscribed=asyncio.Event()
        self.reads=[]
    async def start(self): pass
    async def close(self): pass
    async def read_capability(self,contract,arguments):
        self.reads.append((contract,arguments))
        return {"ok":True,"fixture":True}
    @contextlib.asynccontextmanager
    async def subscribe(self):
        async with super().subscribe() as queues:
            self.subscribed.set()
            yield queues

class BrowserTransport(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.rover=FixtureRover()
        self.bridge=SiteBridge(self.rover,"s"*64,{"forward":1,"reverse":2,"left":3,"right":4})
        self.client=TestClient(TestServer(self.bridge.app()))
        await self.client.start_server()
        self.headers={"Authorization":"Bearer "+"s"*64}
    async def asyncTearDown(self):
        await self.client.close()

    async def test_real_http_auth_routes_and_video_dispatch(self):
        response=await self.client.get("/api/state")
        self.assertEqual(response.status,401)
        response=await self.client.get("/api/state",headers=self.headers)
        self.assertEqual(response.status,200)
        self.assertEqual((await response.json())["lease_owner"],"server")
        response=await self.client.post("/api/video",headers=self.headers,json={"action":"list"})
        self.assertEqual(response.status,200)
        self.assertEqual(self.rover.reads,[("robot.video.import.read:1",{"request":{"action":"list"}})])
        routes={route.resource.canonical for route in self.client.server.app.router.routes()}
        self.assertTrue({"/api/state","/api/controls","/api/led","/api/video","/api/map/snapshot","/api/live/{lane}"}.issubset(routes))

    async def test_real_vision_websocket_preserves_binary_frame(self):
        socket=await self.client.ws_connect("/api/live/vision",headers=self.headers)
        await self.rover.subscribed.wait()
        payload=frame({"type":"frame","processed_monotonic":1.0},b"fixture-jpeg")
        self.rover.broadcast("vision",payload)
        message=await asyncio.wait_for(socket.receive(),3)
        self.assertEqual(message.type,web.WSMsgType.BINARY)
        self.assertEqual(message.data,payload)
        await socket.close()

if __name__=="__main__": unittest.main(verbosity=2)
