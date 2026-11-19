import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from biomerte.host_io import AnchorHub, HostIO, make_server
from biomerte.host_io_client import ClockBounds, HostIOClient, read_exact


class ClockTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.]
        self.bound = ClockBounds("boot-a", 900., 900.02, 100.,
                                 clock=lambda: self.now[0], host_id="registered-host")

    def test_vm_epoch_bounds_and_preserved_source(self):
        low, high = self.bound.age(999.8, "boot-a")
        self.assertAlmostEqual(low, .2)
        self.assertAlmostEqual(high, .22)
        source = {"capture_host_monotonic_interval_s": [999.7, 999.8],
                  "lidar": {"received_monotonic": 999.75},
                  "sensor_packet": {"capture_monotonic_us": 4567},
                  "receive_decode_timestamp": "2026-11-19T22:00:00Z"}
        result = self.bound.metadata(source, "boot-a")
        self.assertEqual(source["lidar"]["received_monotonic"], 999.75)
        self.assertAlmostEqual(result["lidar"]["received_monotonic"], 99.73)
        self.assertEqual(result["sensor_packet"], source["sensor_packet"])
        self.assertEqual(result["receive_decode_timestamp"], source["receive_decode_timestamp"])
        self.assertEqual(result["_host_io_clock"]["source_fields"]["lidar.received_monotonic"], 999.75)
        interval = result["capture_host_monotonic_interval_s"]
        self.assertAlmostEqual(interval[0], 99.68)
        self.assertAlmostEqual(interval[1], 99.8)

    def test_reboot_expiry_uncertainty_and_wall_skew_fail_closed(self):
        with self.assertRaises(ValueError):
            self.bound.age(999., "boot-b")
        self.now[0] = 111.
        self.assertFalse(self.bound.valid)
        with self.assertRaises(ValueError):
            self.bound.age(999.)
        self.assertFalse(ClockBounds("a", 0., .3, 0., clock=lambda: 0.).valid)
        self.assertFalse(ClockBounds("a", 0., .01, 0., clock=lambda: 0.,
                                    wall_offset_low=2., wall_offset_high=2.01).valid)

    def test_partial_reads_are_assembled_and_truncation_rejected(self):
        class Partial(io.BytesIO):
            def read(self, n=-1):
                return super().read(min(n, 2))
        self.assertEqual(read_exact(Partial(b"abcdef"), 6), b"abcdef")
        with self.assertRaises(EOFError):
            read_exact(Partial(b"abc"), 4)


class Routes:
    def __init__(self, path):
        self.rows = {"camera": {"device_id": "camera", "host": "192.168.50.4",
                                "revision": 4, "identity": {"mac": "aa:bb:cc:dd:ee:ff"}}}
    def list(self):
        return list(self.rows.values())
    def get(self, device_id):
        return self.rows.get(device_id)
    def observe(self, device_id, host, identity):
        row = self.rows.get(device_id)
        if row and any(key in row["identity"] and row["identity"][key] != value
                       for key, value in identity.items()):
            raise ValueError("identity changed")
        value = dict(device_id=device_id, host=host, revision=(row["revision"] + 1) if row else 1,
                     identity=dict(identity))
        self.rows[device_id] = value
        return value


class RouteModule:
    DeviceRoutes = Routes
    @staticmethod
    def refresh_neighbors(registry, neighbors):
        return []
    @staticmethod
    def provision_routes(registry, config):
        return []


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.token = self.path / "token"
        self.token.write_text("test-only-credential-" * 4)
        self.token.chmod(0o600)
        (self.path / "routes.sqlite3").touch()
        self.settings = dict(host_id="registered-host", token_file=str(self.token),
                             device_routes=str(self.path / "routes.sqlite3"),
                             vision_directory=str(self.path), listen="127.0.0.1", port=0)
        self.backend = HostIO(self.settings, routes_module=RouteModule,
                              neighbors=lambda: [], clock_id="registered-host:boot-one")
        self.server = make_server(self.settings, self.backend)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.client = HostIOClient(dict(url=self.url, token_file=str(self.token),
                                       allow_local_http=True, host_id="registered-host"))

    def tearDown(self):
        self.backend.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def test_authenticated_route_clock_and_dynamic_resolution(self):
        bounds = self.client.clock()
        self.assertTrue(bounds.valid)
        self.assertEqual(bounds.host_id, "registered-host")
        self.assertEqual(bounds.clock_id, "registered-host:boot-one")
        self.assertEqual(self.client.origin("http://old.invalid:80/path", "camera"),
                         "http://192.168.50.4:80/path")
        self.backend.routes.rows["camera"]["host"] = "192.168.50.9"
        self.assertEqual(self.client.route("camera")["host"], "192.168.50.9")
        with self.assertRaises(OSError):
            self.client.origin("http://old.invalid", "unknown")

    def test_auth_effect_gate_host_binding_and_endpoint_allowlist(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(self.url + "/v1/host-io/anchors", timeout=2)
        self.assertEqual(error.exception.code, 401)
        with self.assertRaises(HTTPError) as error:
            self.client.observe_anchor("aa:bb:cc:dd:ee:ff", "192.168.50.7")
        self.assertEqual(error.exception.code, 403)
        with self.assertRaises(HTTPError) as error:
            self.client._call("shell", argv=["false"])
        self.assertEqual(error.exception.code, 400)
        wrong = HostIOClient(dict(url=self.url, token_file=str(self.token),
                                 allow_local_http=True, host_id="wrong-host"))
        with self.assertRaises(ValueError):
            wrong.clock()

    def test_anchor_candidates_retain_native_station_variants(self):
        self.backend.neighbors = lambda: [
            ("192.168.50.10", "a2:bb:cc:dd:ee:10"),
            ("192.168.50.11", "a2:bb:cc:dd:ee:f"),
            ("192.168.50.12", "a0:bb:cc:dd:ee:10"),
            ("192.168.50.13", "a2:bb:cc:dd:ee:11")]
        self.assertEqual(self.client.candidates("a2:bb:cc:dd:ee:10"),
                         ["192.168.50.10", "192.168.50.11", "192.168.50.12"])
        self.backend.settings["enable_effects"] = True
        row = self.client.observe_anchor("a2:bb:cc:dd:ee:10", "192.168.50.10")
        self.assertEqual(row["identity"], {"kind": "ftm_anchor", "bssid": "a2:bb:cc:dd:ee:10"})

    def test_existing_survey_protocol_is_forwarded_with_same_request_id(self):
        path = self.path / "survey-pose.sock"
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(str(path))
            sock.listen(1)
            seen = []
            def serve():
                peer, _ = sock.accept()
                with peer, peer.makefile("rb") as source:
                    body = json.loads(source.readline(2048))
                    seen.append(body)
                    peer.sendall(json.dumps(dict(ok=True, reset_id=body["request_id"])).encode() + b"\n")
            worker = threading.Thread(target=serve, daemon=True)
            worker.start()
            self.backend.settings["enable_effects"] = True
            request_id = "12345678-1234-1234-1234-123456789abc"
            result = self.client.survey_zero("world-one", request_id)
            worker.join(timeout=2)
            self.assertEqual(result["reset_id"], request_id)
            self.assertEqual(seen, [dict(type="request", world_id="world-one", request_id=request_id)])

    def test_native_vision_metadata_jpeg_and_clock_are_preserved(self):
        path = self.path / "vision.sock"
        metadata = dict(sensor_tick=True, capture_host_monotonic_interval_s=[
                        time.monotonic() - .1, time.monotonic() - .09],
                        sensor_packet={"capture_monotonic_us": 1234})
        import struct
        encoded = json.dumps(metadata).encode()
        jpeg = b"\xff\xd8fixture\xff\xd9"
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(str(path))
            sock.listen(1)
            def serve():
                peer, _ = sock.accept()
                with peer:
                    request = b""
                    while b"\r\n\r\n" not in request:
                        request += peer.recv(1024)
                    peer.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: application/octet-stream\r\n\r\n")
                    peer.sendall(struct.pack("!II", 4+len(encoded)+len(jpeg), len(encoded)) + encoded + jpeg)
            worker = threading.Thread(target=serve, daemon=True)
            worker.start()
            stream = self.client.vision_frames()
            value, image = next(stream)
            stream.close()
            worker.join(timeout=2)
            self.assertEqual(image, jpeg)
            self.assertEqual(value["sensor_packet"], metadata["sensor_packet"])
            self.assertEqual(value["_host_io_clock"]["source_fields"][
                "capture_host_monotonic_interval_s"], metadata["capture_host_monotonic_interval_s"])


class UDPTests(unittest.TestCase):
    def test_host_endpoint_keeps_peer_and_rejects_unauthorized_destinations(self):
        hub = AnchorHub(dict(listen="127.0.0.1", port=0), "boot-native")
        hub.start()
        try:
            port = hub.sock.getsockname()[1]
            # Runtime config uses a fixed port. Test assigns the ephemeral bound port.
            hub.settings["port"] = port
            with hub.subscribe() as inbox, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
                peer.bind(("127.0.0.1", 0))
                peer.sendto(b"packet fragment", ("127.0.0.1", port))
                row = inbox.get(timeout=2)
                import base64
                self.assertEqual(base64.b64decode(row["payload"]), b"packet fragment")
                self.assertEqual(row["peer"], list(peer.getsockname()))
                self.assertEqual(row["clock_id"], "boot-native")
            record = dict(schema="rover.tof.responder/1", bssid="aa:bb:cc:dd:ee:ff",
                          ssid="C3-TOF-test", channel=6, ftm_responder=True)
            with self.assertRaises(ValueError):
                hub.send(record, "192.0.2.99", port)
            with self.assertRaises(ValueError):
                hub.send(dict(record, command="other"), "127.0.0.1", port)
        finally:
            hub.close()

if __name__ == "__main__":
    unittest.main()
