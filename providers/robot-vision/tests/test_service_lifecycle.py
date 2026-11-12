"""Lifecycle boundaries for optional native app ownership and legacy startup."""
import contextlib
import signal
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

import install_provider as installer
import vision_service as runtime


class ServiceModeTests(unittest.TestCase):
    def configuration(self, **kwargs):
        return installer.make_config("/tmp/repo", "/tmp/vision", "/tmp/gateway",
                                     {"site": "test-site"}, {"node": "test-node"}, **kwargs)

    def test_named_app_is_explicit_and_selection_survives_reinstall(self):
        original = self.configuration()
        self.assertEqual(original["service_mode"], "python")
        selected = self.configuration(previous=original, service_mode="macos-app")
        reinstalled = self.configuration(previous=selected)
        self.assertEqual(reinstalled["service_mode"], "macos-app")
        jobs = installer.launch_agents(reinstalled, "/tmp/test-home")
        service, provider = jobs[installer.SERVICE_LABEL], jobs[installer.PROVIDER_LABEL]
        self.assertEqual(service["ProgramArguments"], [
            str(Path("/tmp/test-home/Applications/Biome Robot Vision.app/Contents/MacOS/BiomeRobotVision").resolve()),
            "--launcher-config", str(Path("/tmp/vision/macos-app/launcher.json").resolve())])
        self.assertEqual(service["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(service["ExitTimeOut"], 0)
        self.assertEqual(provider["ProgramArguments"][0], str(Path("/tmp/vision/env/bin/python").resolve()))
        self.assertTrue(provider["KeepAlive"])

    def test_unknown_service_mode_does_not_silently_change_ownership(self):
        with self.assertRaises(ValueError):
            self.configuration(service_mode="mistyped")


class ParentLifecycleTests(unittest.TestCase):
    def run_service(self, *, native_parent, lose_parent_during_init=False):
        events = []
        stop = threading.Event()
        parent = [43210]
        thread_api = types.SimpleNamespace(Event=lambda: stop, Thread=threading.Thread)

        class Service:
            def __init__(self, *_args, defer_backend_start=False):
                if not defer_backend_start:
                    raise AssertionError('daemon must defer backend restoration until capture is available')
                self.live_frames=types.SimpleNamespace(close=lambda:events.append("live_frames_closed"))
                self.live_maps=types.SimpleNamespace(close=lambda:events.append("live_maps_closed"))
                events.append("initialized")
                if lose_parent_during_init:
                    parent[0] = 1
                    if not stop.wait(10):
                        raise AssertionError("Parent monitor did not request shutdown during initialization")

            def start(self):
                events.append("started")
                stop.set()

            def close(self):
                events.append("closed")

        class Server:
            def __init__(self, address, *_args):
                events.append("api_created")
                Path(address).touch()

            def serve_forever(self):
                pass

            def shutdown(self):
                events.append("api_stopped")

            def server_close(self):
                events.append("api_closed")

        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(runtime, "threading", thread_api))
            stack.enter_context(mock.patch.object(runtime, "VisionService", Service))
            stack.enter_context(mock.patch.object(runtime, "LocalServer", Server))
            stack.enter_context(mock.patch.object(runtime, "load_config", return_value={}))
            stack.enter_context(mock.patch.object(runtime.os, "umask"))
            stack.enter_context(mock.patch.object(runtime.signal, "signal", return_value=signal.SIG_DFL))
            ppid = stack.enter_context(mock.patch.object(runtime.os, "getppid", side_effect=lambda: parent[0]))
            arguments = ["--state", directory]
            if native_parent:
                arguments += ["--parent-pid", "43210"]
            runtime.main(arguments)
            if not native_parent:
                ppid.assert_not_called()
            self.assertFalse((Path(directory) / "vision.sock").exists())
        return events

    def test_direct_service_runs_without_parent_monitor(self):
        events = self.run_service(native_parent=False)
        self.assertIn("started", events)
        self.assertLess(events.index("live_frames_closed"),events.index("api_closed"))
        self.assertLess(events.index("live_maps_closed"),events.index("api_closed"))
        self.assertEqual(events[-1], "closed")

    def test_live_native_parent_allows_start_and_ordinary_shutdown(self):
        events = self.run_service(native_parent=True)
        self.assertIn("started", events)
        self.assertEqual(events[-1], "closed")

    def test_parent_death_during_initialization_closes_without_publishing_api(self):
        events = self.run_service(native_parent=True, lose_parent_during_init=True)
        self.assertEqual(events, ["initialized", "closed"])


if __name__ == "__main__":
    unittest.main()

