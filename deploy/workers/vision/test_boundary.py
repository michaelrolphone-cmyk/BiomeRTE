"""Exercise actual leased stream routing without a gateway, camera or native socket."""
import asyncio
import importlib.util
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT/"providers/robot-vision")]
from leased_streams import VisionStreams

class WireContext:
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False

class Remote:
    def call(self, operation, arguments):
        if operation != "stream.attach":
            raise AssertionError(operation)
        return {"attachment_credential":"fixture-only", "url":"wss://fixture.invalid/stream"}

class StreamBoundaryTests(unittest.TestCase):
    def test_front_rear_and_map_keep_separate_native_feeds(self):
        for resource, lane, expected in (("rover","vision","live"),
                                        ("rover.rear","vision","rear"),
                                        ("rover","map","map"),
                                        ("rover.rear","map","map")):
            with self.subTest(resource=resource, lane=lane), tempfile.TemporaryDirectory() as directory:
                stop = threading.Event()
                opened = []
                closed = []
                class Source:
                    def open(self):
                        stop.set()
                        return self
                    def close(self):
                        closed.append(True)
                    abort = close
                class Client:
                    def open_vision_stream(self, stream):
                        opened.append(stream)
                        return Source()
                streams = VisionStreams(Remote(), "/missing.sock",
                                        sequence_path=Path(directory)/"sequences.sqlite",
                                        rear_resource="rover.rear",
                                        host_io_config="/fixture.json")
                grant = {"id":"session", "resource":resource, "lane":lane}
                with patch("websockets.asyncio.client.connect", return_value=WireContext()), \
                     patch("biomerte.host_io_client.HostIOClient.from_config", return_value=Client()):
                    asyncio.run(streams.run(grant, stop))
                self.assertEqual(opened, [expected])
                self.assertTrue(closed)
                self.assertFalse(streams.connections)

    def test_health_deadline_reaches_the_native_query_transport(self):
        import io
        import json
        from biomerte.host_io_client import HostIOClient, PROTOCOL
        import biome_provider
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory)/"token"
            token.write_text("fixture-token-with-at-least-32-characters")
            seen = []
            def opener(request, timeout):
                operation = json.loads(request.data)["operation"]
                seen.append((operation, timeout))
                result = ({"host_id":"fixture", "clock_id":"boot", "monotonic":100.0,
                           "wall_time":1000.0} if operation == "clock" else
                          {"status":200, "body":{"state":"ready"}})
                return io.BytesIO(json.dumps({"protocol":PROTOCOL, "host_id":"fixture",
                                              "clock_id":"boot", "result":result}).encode())
            native = HostIOClient({"url":"https://fixture.invalid", "token_file":str(token)},
                                  opener=opener, monotonic=lambda:100.0, wall=lambda:1000.0)
            with patch("biomerte.host_io_client.HostIOClient.from_config", return_value=native):
                client = biome_provider.VisionClient("/unmounted.sock", 5.0, "/fixture-config")
                self.assertEqual(client.query("robot.vision.status", {})["state"], "ready")
            self.assertIn(("vision.query", 5.0), seen)

    def test_manifest_render_preserves_identity_and_persistent_state(self):
        spec = importlib.util.spec_from_file_location("vision_config_render", Path(__file__).with_name("render-config.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        native = {"provider":"p", "site":"s", "resource":"r", "slots":3,
                  "state":"/native/vision", "gateway_state":"/native/oauth",
                  "socket":"/native/vision/vision.sock", "python":"/native/python"}
        rendered = module.render(native)
        for key in ("provider", "site", "resource", "slots", "state"):
            self.assertEqual(native[key], rendered[key])
        self.assertEqual(rendered["gateway_source"], "/app/gateway")
        self.assertEqual(rendered["host_io_config"], "/run/biome/host-io.json")
        self.assertNotIn("gateway_state", rendered)
        self.assertEqual(native["python"], "/native/python")

if __name__ == "__main__":
    unittest.main()
