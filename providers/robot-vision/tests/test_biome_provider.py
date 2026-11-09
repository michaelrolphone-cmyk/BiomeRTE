"""Adapter boundary tests: coherence, artifact integrity, isolation, and lifecycle."""
import base64
import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler
import io
import json
import os
from pathlib import Path
import plistlib
import socket
import socketserver
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import biome_provider as adapter
import install_provider as installer


class FakeRemote:
    def __init__(self, bad_checksum=False):
        self.calls = []
        self.bad_checksum = bad_checksum

    def call(self, name, args, rid=None):
        self.calls.append((name, args))
        data = base64.b64decode(args["data"])
        return {"sha256": "0" * 64 if self.bad_checksum else hashlib.sha256(data).hexdigest()}


class FakeClient:
    def __init__(self, response):
        self.response, self.calls = response, []

    def query(self, operation, inputs):
        self.calls.append((operation, inputs))
        return self.response


class QueryTests(unittest.TestCase):
    def payload(self, data=b"jpeg-test-frame"):
        return {
            "snapshot_id": "snapshot-7", "map_id": "persistent-map", "map_revision": 11,
            "pose": {"frame_id": "frame-7", "state": "unlocalized"},
            "frame": {"frame_id": "frame-7", "captured_at": "2026-11-09T00:00:00Z",
                      "data_base64": base64.b64encode(data).decode(),
                      "sha256": hashlib.sha256(data).hexdigest()},
        }

    def test_default_snapshot_is_coherent_and_never_uploads_image(self):
        payload = self.payload()
        client, remote = FakeClient(payload), FakeRemote()
        result = adapter.execute({"contract": "robot.vision.snapshot:1", "inputs": {}},
                                 {}, remote, client)["result"]
        self.assertEqual(client.calls, [("robot.vision.snapshot", {})])
        self.assertEqual(result["snapshot_id"], "snapshot-7")
        self.assertEqual(result["map_revision"], 11)
        self.assertEqual(result["pose"]["frame_id"], result["frame"]["frame_id"])
        self.assertNotIn("data_base64", result["frame"])
        self.assertEqual(remote.calls, [])
        self.assertIn("data_base64", payload["frame"], "Do not mutate the service's retained snapshot.")

    def test_requested_image_uploads_verified_bounded_chunks(self):
        data = bytes(range(256)) * 401
        remote = FakeRemote()
        result = adapter.execute(
            {"contract": "robot.vision.snapshot:1", "inputs": {"snapshot_id": "snapshot-7", "include_frame": True}},
            {}, remote, FakeClient(self.payload(data)))["result"]
        artifact = result["frame"]["artifact"]
        reconstructed = b"".join(base64.b64decode(args["data"]) for _, args in remote.calls)
        self.assertEqual(reconstructed, data)
        self.assertEqual(artifact["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(artifact["size"], len(data))
        self.assertEqual(result["frame"]["byte_count"], len(data))
        self.assertEqual(sum(chunk["size"] for chunk in artifact["chunks"]), len(data))
        self.assertTrue(all(chunk["size"] <= adapter.CHUNK_BYTES for chunk in artifact["chunks"]))
        self.assertNotIn("data_base64", json.dumps(result))

    def test_frame_operation_uploads_only_its_exact_image(self):
        payload = self.payload()
        payload["debug"] = [{"data_base64": "never-forward-me"}]
        remote = FakeRemote()
        result = adapter.execute({"contract": "robot.vision.frame:1", "inputs": {"frame_id": "frame-7"}},
                                 {}, remote, FakeClient(payload))["result"]
        self.assertEqual(len(remote.calls), 1)
        self.assertEqual(result["frame"]["frame_id"], "frame-7")
        self.assertNotIn("data_base64", json.dumps(result))

    def test_corrupt_chunk_reply_and_local_digest_are_rejected(self):
        with self.assertRaisesRegex(adapter.ServiceError, "chunk checksum"):
            adapter.externalize_frame(self.payload(), FakeRemote(True), True)
        payload = self.payload()
        payload["frame"]["sha256"] = "wrong"
        remote = FakeRemote()
        with self.assertRaisesRegex(adapter.ServiceError, "frame checksum"):
            adapter.externalize_frame(payload, remote, True)
        self.assertEqual(remote.calls, [])

    def test_invalid_inline_image_is_not_misrepresented_as_artifact(self):
        payload = self.payload()
        payload["frame"]["data_base64"] = "not base64!"
        with self.assertRaises(adapter.ServiceError):
            adapter.externalize_frame(payload, FakeRemote(), True)

    def test_advertisement_requires_service_supported_operation(self):
        self.assertEqual(adapter.supported_contracts({}), set())
        status = {"capabilities": ["robot.vision.status", "robot.map.read:1", "robot.map.open:2", "robot.drive:1"]}
        self.assertEqual(adapter.supported_contracts(status),
                         {"robot.vision.status:1", "robot.map.read:1"})

    def test_handled_query_error_does_not_poison_worker_slot(self):
        output = io.StringIO()
        with mock.patch.object(adapter, "load_config", return_value={}), \
                mock.patch.object(adapter, "Remote"), \
                mock.patch.object(adapter, "execute", side_effect=adapter.ServiceError("uncalibrated", "Need intrinsics.")), \
                mock.patch.object(sys, "stdin", io.StringIO('{"operation":"query-1","inputs":{}}')), \
                contextlib.redirect_stdout(output):
            code = adapter.main(["execute", "--contract", "robot.vision.project_ground:1"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["result"]["error"]["code"], "uncalibrated")

    def test_mutation_transport_error_reports_unknown_effect(self):
        output = io.StringIO()
        with mock.patch.object(adapter, "load_config", return_value={}), \
                mock.patch.object(adapter, "Remote"), \
                mock.patch.object(adapter, "execute", side_effect=adapter.ServiceError("service_unavailable", "Unavailable.")), \
                mock.patch.object(sys, "stdin", io.StringIO('{"operation":"mutation-1","inputs":{}}')), \
                contextlib.redirect_stdout(output):
            self.assertEqual(adapter.main(["execute", "--contract", "robot.map.create:1"]), 0)
        self.assertEqual(json.loads(output.getvalue())["result"]["error"]["effect"], "unknown")


class TemporaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rv-adapter-")
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.addCleanup(self.temp.cleanup)

    def config(self, **kwargs):
        return installer.make_config(self.root / "repo", self.root / "state",
                                     self.root / "gateway", {"site": "site-one"},
                                     {"node": "a" * 64}, **kwargs)


class SocketTests(TemporaryTests):
    def make_server(self, response, status=200):
        events = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                events.append({"path": self.path, "body": data})
                encoded = json.dumps(response).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *_args):
                pass

        path = self.root / "s"
        server = socketserver.UnixStreamServer(str(path), Handler)
        path.chmod(0o600)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.addCleanup(cleanup)
        return adapter.VisionClient(path, timeout=2), events

    def test_real_unix_http_uses_the_direct_service_envelope(self):
        client, events = self.make_server({"snapshot_id": "s1", "frame": {"frame_id": "f1"}})
        result = client.query("robot.vision.snapshot", {"snapshot_id": "s1"})
        self.assertEqual(result["snapshot_id"], "s1")
        self.assertEqual(events, [{"path": "/v1/query",
                                  "body": {"operation": "robot.vision.snapshot",
                                           "inputs": {"snapshot_id": "s1"}}}])

    def test_http_error_remains_an_explicit_domain_error(self):
        client, _ = self.make_server({"error": {"code": "uncalibrated", "message": "Need intrinsics."}}, 409)
        with self.assertRaises(adapter.ServiceError) as found:
            client.query("robot.vision.project_ground",
                         {"pixel": [2, 3], "confirmed_floor_contact": True})
        self.assertEqual(found.exception.code, "uncalibrated")

    def test_oversized_service_reply_is_bounded(self):
        client, _ = self.make_server({"payload": "x" * 100})
        with mock.patch.object(adapter, "MAX_RESPONSE_BYTES", 32), \
                self.assertRaises(adapter.ServiceError) as found:
            client.query("robot.vision.status", {})
        self.assertEqual(found.exception.code, "response_too_large")

    def test_socket_permissions_and_symlinks_are_rejected(self):
        path = self.root / "s"
        with socket.socket(socket.AF_UNIX) as local:
            local.bind(str(path))
            path.chmod(0o666)
            with self.assertRaises(adapter.ServiceError):
                adapter.check_socket(path)
            path.chmod(0o600)
            adapter.check_socket(path)
            link = self.root / "link"
            link.symlink_to(path)
            with self.assertRaises(adapter.ServiceError):
                adapter.check_socket(link)


class LifecycleTests(TemporaryTests):
    def test_own_provider_and_resource_are_stable_and_distinct(self):
        cfg = self.config()
        self.assertNotEqual(cfg["provider"], "a" * 64)
        self.assertTrue(cfg["resource"].startswith("robotvision.mac."))
        self.assertEqual(self.config(previous=cfg)["provider"], cfg["provider"])
        previous = dict(cfg, site="a-different-site")
        with self.assertRaisesRegex(ValueError, "identity changed"):
            self.config(previous=previous)

    def test_registration_record_is_durable_and_bound_to_own_authority(self):
        cfg = self.config()
        path = self.root / "registered.json"
        contracts = {"robot.vision.status:1", "robot.map.read:1"}
        adapter.write_registered(path, cfg, contracts)
        self.assertEqual(adapter.read_registered(path, cfg), contracts)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        with self.assertRaisesRegex(RuntimeError, "another provider authority"):
            adapter.read_registered(path, dict(cfg, provider="other-provider"))

    def test_contracts_are_accepted_by_controller_schema(self):
        repo = str(Path(__file__).resolve().parents[3])
        with mock.patch.object(sys, "path", [repo] + sys.path):
            from biomerte.api import schema, validate
            from biomerte.capability_usage import published_usage
        parsed = {}
        for name, definition in adapter.CONTRACTS.items():
            with self.subTest(contract=name):
                parsed[name] = schema(definition["inputs"])
                validate(schema(definition["outputs"]), {"result": {}})
                guide = adapter.offer({"provider": "test-provider", "resource": "test-resource",
                                       "slots": 4}, name, "ready")["usage"]
                published_usage(dict(definition, inputs=parsed[name]), guide)
        validate(parsed["robot.vision.snapshot:1"], {})
        validate(parsed["robot.map.read:1"], {"limit": 5})
        validate(parsed["robot.vision.calibration:1"], {"action": "get"})
        self.assertFalse(parsed["robot.vision.snapshot:1"]["include_frame"]["required"])
        self.assertTrue(parsed["robot.map.changes:1"]["since_revision"]["required"])
        self.assertTrue(parsed["robot.vision.project_ground:1"]["confirmed_floor_contact"]["required"])

    def test_each_worker_offer_binds_its_contract_in_argv(self):
        cfg = self.config()
        offers = adapter.worker_offers(cfg)
        self.assertEqual(len(offers), 13)
        for item in offers:
            self.assertEqual(item["argv"][-2:], ["--contract", item["contract"]])
            self.assertEqual(item["argv"][0], cfg["python"])
            self.assertEqual(item["resource"], cfg["resource"])

    def test_launch_agents_use_the_user_environment_and_separate_processes(self):
        cfg = self.config()
        jobs = installer.launch_agents(cfg, self.root)
        self.assertEqual(set(jobs), {"biome.robot-vision", "biome.robot-vision-provider"})
        for spec in jobs.values():
            self.assertEqual(spec["ProgramArguments"][0], str((self.root / "state/env/bin/python").resolve()))
            self.assertEqual(spec["EnvironmentVariables"]["HOME"], str(self.root))
            self.assertTrue(spec["KeepAlive"])
        self.assertIn("vision_service.py", jobs[installer.SERVICE_LABEL]["ProgramArguments"][1])
        self.assertIn("biome_provider.py", jobs[installer.PROVIDER_LABEL]["ProgramArguments"][1])
        self.assertNotEqual(jobs[installer.SERVICE_LABEL]["StandardOutPath"],
                            jobs[installer.PROVIDER_LABEL]["StandardOutPath"])

    def test_install_without_start_writes_private_files_and_does_not_launch(self):
        cfg = self.config()
        with mock.patch.object(installer, "validate_install"), mock.patch.object(installer.subprocess, "run") as run:
            result = installer.install(cfg, start=False, home=self.root)
        run.assert_not_called()
        self.assertFalse(result["started"])
        manifest = Path(cfg["state"]) / "provider-manifest.json"
        self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o600)
        self.assertEqual(json.loads(manifest.read_text())["provider"], cfg["provider"])
        for label in (installer.SERVICE_LABEL, installer.PROVIDER_LABEL):
            path = self.root / "Library/LaunchAgents" / (label + ".plist")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(plistlib.loads(path.read_bytes())["Label"], label)

    def test_default_installer_plan_has_no_write_or_launch_side_effect(self):
        gateway = self.root / "gateway"
        (gateway / "identity").mkdir(parents=True)
        (gateway / "terminal-manifest.json").write_text('{"site":"site-one"}')
        (gateway / "identity/node.json").write_text(json.dumps({"node": "a" * 64}))
        output = io.StringIO()
        with mock.patch.object(installer, "install") as install, contextlib.redirect_stdout(output):
            self.assertEqual(installer.main(["--repo", str(self.root / "repo"),
                                            "--state", str(self.root / "new-state"),
                                            "--gateway-state", str(gateway)]), 0)
        install.assert_not_called()
        self.assertFalse(json.loads(output.getvalue())["installed"])
        self.assertFalse((self.root / "new-state").exists())


if __name__ == "__main__":
    unittest.main()
