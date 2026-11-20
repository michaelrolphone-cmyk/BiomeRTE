"""Native dependency forwarding and cross-VM timing, without live credentials."""
import io
import json
import os
from pathlib import Path
import socket
import socketserver
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler

from biomerte.host_io import HostIO, make_server
from biomerte.host_io_client import HostIOClient, VisionFrameStream, VisionQueryError


class WorkerHostIOTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.backend = HostIO.__new__(HostIO)
        self.backend.settings = {}
        self.backend._gateway = None
        self.backend._gitlab = None
        self.backend._gateway_lock = threading.Lock()
        self.backend.host_id = "native-mac"
        self.backend.clock_id = "native-mac:boot-fixture"
        self.backend.routes = SimpleNamespace(list=lambda: [], get=lambda **kw: None)
        self.backend.anchors = SimpleNamespace(send=lambda **kw: None)

    def tearDown(self):
        self.temp.cleanup()

    def test_gateway_keeps_request_id_context_and_result(self):
        module = self.root / "native_gateway.py"
        module.write_text(
            "calls=[]\n"
            "def invoke(operation, arguments=None, rid=None, realm='site', provider_context=None):\n"
            " calls.append((operation,arguments,rid,realm,provider_context))\n"
            " return {'received':calls[-1], 'result':{'ok':True}}\n")
        self.backend.settings["gateway"] = {"enabled": True, "module": str(module)}
        context = {"provider": "a"*64, "host": "Mac-mini", "addresses": ["192.0.2.2"]}
        result = self.backend.gateway_invoke("provider.complete", {"action": "original"},
                                            "stable-request", "local", context)
        self.assertEqual(result["received"],
                         ("provider.complete", {"action":"original"}, "stable-request", "local", context))
        self.assertEqual(len(self.backend._gateway.calls), 1)

    def test_forwarding_requires_explicit_native_bindings(self):
        with self.assertRaises(PermissionError):
            self.backend.gateway_invoke("cap.discover", {})
        with self.assertRaises(PermissionError):
            self.backend.gitlab_token()
        self.backend.settings["gateway"] = {"enabled": True, "module": "relative.py"}
        with self.assertRaises(ValueError):
            self.backend.gateway_invoke("cap.discover", {})

    def test_gitlab_uses_native_token_function(self):
        module = self.root / "native_gitlab.py"
        module.write_text("calls=0\ndef token():\n global calls\n calls+=1\n return 'fixture-only-token'\n")
        self.backend.settings["gitlab"] = {"enabled": True, "module": str(module)}
        self.assertEqual(self.backend.call("gitlab.token", {}), "fixture-only-token")
        self.assertEqual(self.backend._gitlab.calls, 1)

    def test_vision_effects_and_contract_allowlist(self):
        self.backend.settings["vision_queries"] = {
            "operations": ["robot.camera.light.set"],
            "mutations": ["robot.camera.light.set"]}
        with self.assertRaises(PermissionError):
            self.backend.vision_query("robot.camera.light.set", {"on":True})
        with self.assertRaises(ValueError):
            self.backend.vision_query("arbitrary.operation", {})
        with self.assertRaises(ValueError):
            self.backend.vision_query("robot.camera.light.set", [])

    def test_video_jobs_run_with_the_configured_native_interpreter(self):
        import sys
        video_source = self.root / "native-video-source"
        video_source.mkdir()
        module = video_source / "video_jobs.py"
        module.write_text("import sys\nclass VideoJobs:\n def __init__(self,state): self.state=state\n"
                          " def handle(self,request): return {'python':sys.executable,'state':self.state,'request':request}\n")
        self.backend.settings.update(
            device_routes_module_directory=str(self.root), vision_directory=str(self.root/"vision"),
            vision_queries={"operations":["robot.video.import.read"],"mutations":[],
                            "python":sys.executable, "module_directory":str(video_source)})
        result = self.backend.vision_query("robot.video.import.read", {"request":{"action":"status"}})
        self.assertEqual(result["body"]["python"], sys.executable)
        self.assertEqual(result["body"]["request"], {"action":"status"})

    def start_server(self, backend):
        token = self.root / "token"
        token.write_text("fixture-auth-" + "x"*40)
        token.chmod(0o600)
        settings = {"token_file": str(token), "listen":"127.0.0.1", "port":0,
                    "gateway":{"enabled":True}}
        server = make_server(settings, backend)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return HostIOClient({"url":"http://127.0.0.1:"+str(server.server_port),
                             "allow_local_http":True, "host_id":backend.host_id,
                             "token_file":str(token)})

    def test_real_http_gateway_large_payload_and_no_added_deadline(self):
        backend = self.backend
        captured = []
        def call(operation, arguments):
            captured.append((operation, arguments))
            return {"echo":arguments, "payload":"z"*(1024*1024+17)}
        backend.call = call
        client = self.start_server(backend)
        payload = {"operation":"cap.invoke", "arguments":{"payload":"q"*(1024*1024+17)},
                   "request_id":"same-operation", "realm":"site"}
        result = client.call("gateway.invoke", payload)
        self.assertEqual(result["echo"], payload)
        self.assertEqual(captured, [("gateway.invoke",payload)])
        self.assertGreater(len(result["payload"]), 1024*1024)

    def test_query_converts_native_clock_and_keeps_native_errors(self):
        backend = self.backend
        def call(operation, arguments):
            now = time.monotonic()+5000.
            if operation == "clock":
                return {"host_id":backend.host_id, "clock_id":backend.clock_id,
                        "monotonic":now, "wall_time":time.time()}
            if arguments["query"] == "robot.vision.status":
                return {"status":200,"body":{"processed_monotonic":now-.1,
                    "frame":{"capture_host_monotonic_interval_s":[now-.2,now-.1]}}}
            return {"status":409,"body":{"error":{"code":"map_busy","message":"Map operation is active."}}}
        backend.call = call
        client = self.start_server(backend)
        result = client.vision_query("robot.vision.status", {})
        self.assertLess(abs(time.monotonic()-result["processed_monotonic"]), .5)
        self.assertTrue(result["_host_io_clock"]["qualified"])
        self.assertGreater(result["_host_io_clock"]["source_fields"]["processed_monotonic"],
                           result["processed_monotonic"]+4999.)
        with self.assertRaises(VisionQueryError) as caught:
            client.vision_query("robot.map.open", {"map_id":"fixture"})
        self.assertEqual(caught.exception.code, "map_busy")

    def test_abort_interrupts_an_idle_frame_read(self):
        left, right = socket.socketpair()
        self.addCleanup(right.close)
        class Response:
            def __init__(self, sock):
                self.sock = sock
                self.source = sock.makefile("rb")
            def read(self, size=-1):
                return self.source.read(size)
            def fileno(self):
                return self.source.fileno()
            def close(self):
                self.source.close()
                self.sock.close()
        source = VisionFrameStream(None, "live")
        source.response = Response(left)
        finished = threading.Event()
        def read():
            try:
                source.read_frame()
            except (EOFError, OSError, ValueError, AttributeError):
                pass
            finally:
                finished.set()
        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        source.abort()
        self.assertTrue(finished.wait(1.), "an idle native frame read must not hold a released lease")
        thread.join()

    def test_frame_attachment_closed_during_open_does_not_leak(self):
        response = io.BytesIO()
        class Client:
            def clock(self): pass
            def _request(self, *args, **kwargs):
                source.abort()
                return response
        source = VisionFrameStream(Client(), "live")
        with self.assertRaises(OSError):
            source.open()
        self.assertTrue(response.closed)


if __name__ == "__main__":
    unittest.main()
