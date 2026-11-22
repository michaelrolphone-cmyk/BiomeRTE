import fcntl
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stream_derivations import StreamDerivations
from biome_provider import ServiceError, vision_service_has_custody, withdraw_unavailable_streams


class StreamReferenceCacheTests(unittest.TestCase):
    def test_adapter_shutdown_preserves_both_offers_while_producer_serves(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = {"state": directory, "provider": "test", "resource": "vision", "slots": 1}
            status = {"state": "switching_map", "streaming": {
                "available": True, "lanes": ["vision", "map"], "rear_enabled": True}}
            client = SimpleNamespace(query=Mock(return_value=status))
            remote = Mock()
            withdraw_unavailable_streams(remote, cfg, client)
            remote.call.assert_not_called()

    def test_adapter_shutdown_withdraws_both_offers_after_confirmed_producer_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = {"state": directory, "provider": "test", "resource": "vision", "slots": 1}
            error = ServiceError("service_unavailable", "missing")
            error.__cause__ = ConnectionRefusedError()
            client = SimpleNamespace(query=Mock(side_effect=error))
            remote = Mock()
            with (Path(directory) / "service.lock").open("wb") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                withdraw_unavailable_streams(remote, cfg, client)
                remote.call.assert_not_called()
            withdraw_unavailable_streams(remote, cfg, client)
            published = [(args[0], args[1]["resource"], args[1]["state"])
                         for args, kwargs in remote.call.call_args_list]
            self.assertEqual(published, [("cap.offer", "vision", "offline"),
                                         ("cap.offer", "vision.rear", "offline")])

    def test_socket_can_be_absent_while_the_vision_service_still_owns_state(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(vision_service_has_custody(directory))
            with (Path(directory) / "service.lock").open("wb") as lock:
                self.assertFalse(vision_service_has_custody(directory))
                fcntl.flock(lock, fcntl.LOCK_EX)
                self.assertTrue(vision_service_has_custody(directory))
            self.assertFalse(vision_service_has_custody(directory))

    def test_live_publication_continues_while_reference_refresh_waits(self):
        with tempfile.TemporaryDirectory() as directory:
            entered, release, published = (threading.Event() for _ in range(3))

            def model():
                entered.set()
                self.assertTrue(release.wait(3))
                return {"definition": {"camera": {}}}

            service = SimpleNamespace(state_dir=directory, _robot_model=model)
            derivations = StreamDerivations(service)
            baseline = Path(directory) / "survey-world.json"
            baseline.write_text(json.dumps({"world_id": "survey"}))
            metadata = {}
            worker = threading.Thread(target=derivations.refresh_references)

            def send():
                derivations.enrich(metadata, b"")
                published.set()

            publisher = threading.Thread(target=send)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                publisher.start()
                self.assertTrue(published.wait(1))
                self.assertIsNone(metadata["world_orientation"]["world_id"])
            finally:
                release.set()
                worker.join(3)
                if publisher.ident is not None:
                    publisher.join(3)
            metadata = {}
            derivations.enrich(metadata, b"")
            self.assertEqual(metadata["world_orientation"]["world_id"], "survey")

    def test_atomic_replacement_updates_cached_world(self):
        with tempfile.TemporaryDirectory() as directory:
            service = SimpleNamespace(state_dir=directory, _robot_model=lambda: {})
            derivations = StreamDerivations(service)
            baseline = Path(directory) / "survey-world.json"
            for name in ("east", "west"):
                temporary = baseline.with_suffix(".new")
                temporary.write_text(json.dumps({"world_id": name}))
                temporary.replace(baseline)
                derivations.refresh_references()
                metadata = {}
                derivations.enrich(metadata, b"")
                self.assertEqual(metadata["world_orientation"]["world_id"], name)

    def test_cached_tof_expires_from_its_real_observation_time(self):
        derivations = StreamDerivations(SimpleNamespace())
        self.assertFalse(derivations.capture_tof()["valid"])
        original = {"valid": True, "collector_age_s": 2, "position": {"x_m": 1},
                    "registration": {"current_camera": {"age_s": 1}}}
        derivations.tof_snapshot = (time.monotonic() - 4, original)
        aged = derivations.capture_tof()
        self.assertFalse(aged["valid"])
        self.assertEqual(aged["status"], "collector_stale")
        self.assertIsNone(aged["position"])
        self.assertGreaterEqual(aged["registration"]["current_camera"]["age_s"], 5)
        self.assertEqual(original["collector_age_s"], 2)
