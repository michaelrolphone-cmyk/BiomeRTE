"""Bounded local stack diagnostics through the actual VisionService dispatcher."""
import asyncio
import json
import os
from contextlib import ExitStack
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import receiver_thread_stacks as stacks
import vision_service
from vision_service import VisionService, LocalServer, handler_for
from test_raw_receiver_status import service_fixture
from sample_thread_stacks import request_snapshot, validate_snapshot

REQUEST = {"receiver_only": True, "thread_stacks": True}


class FakeFrame:
    def __init__(self, back=None, text="fixture"):
        self.f_code = SimpleNamespace(co_filename=text, co_name=text)
        self.f_lineno = 123
        self.f_back = back

    @property
    def f_locals(self):
        raise AssertionError("frame values must never be read")


def chain(depth, text):
    frame = None
    for _ in range(depth):
        frame = FakeFrame(frame, text)
    return frame


class ReceiverThreadStackTests(unittest.TestCase):
    def test_actual_unix_dispatch_bypasses_service_and_publication_locks(self):
        # No service constructor, camera, watcher or backend is started.
        service = object.__new__(VisionService)
        locks = [threading.Lock() for _ in range(6)]
        service._pipeline_lock = locks[0]
        service.atlas = SimpleNamespace(lock=locks[1])
        service.store = SimpleNamespace(_lock=locks[2], _checkpoint_lock=locks[3])
        service._live_camera = SimpleNamespace(_lock=locks[4])
        service.camera_rig = SimpleNamespace(publisher=SimpleNamespace(_lock=locks[5]))
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "stacks.sock")
            server = LocalServer(path, handler_for(service))
            serving = threading.Thread(target=server.serve_forever, daemon=True)
            serving.start()
            try:
                with ExitStack() as held:
                    for lock in locks:
                        held.enter_context(lock)
                    with patch("vision_service.dispatch",
                               side_effect=AssertionError("legacy path touched")):
                        value, response_bytes = asyncio.run(
                            asyncio.wait_for(request_snapshot(path), timeout=1))
                        validate_snapshot(value, os.getpid())
                        self.assertLessEqual(response_bytes, stacks.MAX_JSON_BYTES)
                        self.assertEqual(value["schema"], "biome.vision.thread-stacks.v1")
                        self.assertGreater(value["threads_recorded"], 0)
                        self.assertLessEqual(value["observation_started_monotonic_s"],
                                             value["observation_finished_monotonic_s"])
            finally:
                server.shutdown()
                serving.join(2)
                server.server_close()
                self.assertFalse(serving.is_alive())

    def test_thread_frame_string_and_wire_byte_bounds_are_explicit(self):
        frames = {index: chain(9, "界"*700) for index in range(8)}
        names = [SimpleNamespace(ident=i, name="界"*300) for i in frames]
        with patch.object(stacks.sys, "_current_frames", side_effect=lambda: dict(frames)), \
             patch.object(stacks.threading, "enumerate", return_value=names), \
             patch.object(stacks, "MAX_THREADS", 4), \
             patch.object(stacks, "MAX_FRAMES_PER_THREAD", 3):
            value = stacks.thread_stack_snapshot()
            self.assertTrue(value["thread_limit_reached"])
            self.assertEqual(value["threads_recorded"], 4)
            self.assertTrue(value["truncated"])
            for thread in value["threads"]:
                self.assertEqual(len(thread["frames"]), 3)
                self.assertTrue(thread["frames_truncated"])
                self.assertTrue(thread["strings_truncated"])
                self.assertEqual(len(thread["name"]), stacks.MAX_NAME_CHARS)
            self.assertLessEqual(len(json.dumps(value, allow_nan=False).encode()), stacks.MAX_JSON_BYTES)
            with patch.object(stacks, "MAX_JSON_BYTES", 1800):
                limited = stacks.thread_stack_snapshot()
            self.assertTrue(limited["byte_limit_reached"])
            self.assertTrue(limited["truncated"])
            self.assertLessEqual(len(json.dumps(limited, allow_nan=False).encode()), 1800)

    def test_real_locations_exclude_local_and_argument_values_and_file_reads(self):
        ready, release = threading.Event(), threading.Event()

        class NoStringification:
            def __repr__(self):
                raise AssertionError("argument value must not be formatted")

        def parked(argument):
            local_value = "LOCAL_PAYLOAD_d5f980a7"
            ready.set()
            release.wait(2)
            self.assertEqual(local_value, "LOCAL_PAYLOAD_d5f980a7")
            self.assertIsInstance(argument, NoStringification)

        worker = threading.Thread(target=parked, args=(NoStringification(),),
                                  name="snapshot-test-worker")
        worker.start()
        try:
            self.assertTrue(ready.wait(1))
            with patch("builtins.open", side_effect=AssertionError("file read")), \
                 patch("linecache.getline", side_effect=AssertionError("source read")):
                value = stacks.thread_stack_snapshot()
            row = next(item for item in value["threads"] if item["ident"] == worker.ident)
            self.assertEqual(row["name"], "snapshot-test-worker")
            self.assertTrue(any(frame["function"] == "parked" for frame in row["frames"]))
            self.assertEqual(set(row), {"ident", "name", "frames", "frames_truncated", "strings_truncated"})
            for thread in value["threads"]:
                for frame in thread["frames"]:
                    self.assertEqual(set(frame), {"filename", "function", "lineno"})
            encoded = json.dumps(value)
            self.assertNotIn("LOCAL_PAYLOAD_d5f980a7", encoded)
            self.assertNotIn("f_locals", encoded)
            self.assertNotIn("argument", {key for thread in value["threads"]
                                         for frame in thread["frames"] for key in frame})
        finally:
            release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())

    def test_literal_shape_and_existing_status_dispatch_are_preserved(self):
        with service_fixture() as service:
            with patch.object(vision_service, "thread_stack_snapshot", return_value={"stack": True}) as snap, \
                 patch("vision_service.dispatch", return_value={"legacy": True}) as legacy:
                for inputs in ({}, {"thread_stacks": True},
                               {"receiver_only": True, "thread_stacks": 1},
                               {"receiver_only": 1, "thread_stacks": True},
                               {"receiver_only": True, "thread_stacks": False},
                               {**REQUEST, "extra": True}):
                    self.assertEqual(service.query("robot.vision.status", inputs), {"legacy": True})
                    self.assertEqual(legacy.call_args.args[2], inputs)
                self.assertEqual(service.query("robot.vision.geometry", REQUEST), {"legacy": True})
                snap.assert_not_called()
                self.assertEqual(service.query("robot.vision.status", REQUEST), {"stack": True})
                snap.assert_called_once_with()
                count = legacy.call_count
                raw = service.query("robot.vision.status", {"receiver_only": True})
                self.assertEqual(raw["schema"], "biome.vision.receiver-status.v1")
                self.assertNotIn("threads", raw)
                provider = service.query("robot.vision.status", {"provider_only": True})
                self.assertEqual(provider["state"], "running")
                self.assertNotIn("threads", provider)
                self.assertEqual(legacy.call_count, count)
                snap.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
