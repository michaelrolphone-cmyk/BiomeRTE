"""Actual publication paths plus bounded diagnostic lifecycle; no camera/backend starts."""
import gc
import http.client
from http.server import HTTPServer
import json
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import weakref

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import publication_trace as trace_module
from publication_trace import PublicationTrace
import stream_derivations
from stream_derivations import StreamDerivations
from dual_camera_feed import DualCameraFrames, RearCameraFrames
from lidar_fusion import LidarFusion, defaults
from sensor_inventory import SensorInventory
from vision_service import VisionService, LocalServer, handler_for


class ManualTimer:
    def __init__(self, duration, callback, args=()):
        self.duration, self.callback, self.args = duration, callback, args
        self.started = self.cancelled = False
        self.daemon = False
    def start(self):
        self.started = True
    def cancel(self):
        self.cancelled = True
    def fire(self):
        self.callback(*self.args)


class Clock:
    def __init__(self):
        self.wall, self.cpu = 10_000_000_000, 1_000_000
    def now(self):
        return self.wall
    def thread(self):
        return self.cpu
    def advance(self, wall, cpu=0):
        self.wall += int(wall*1e9)
        self.cpu += int(cpu*1e9)


def identity(frame, camera="rear"):
    return dict(frame_id=frame, camera_id=camera, source_id=camera+"-camera",
                boot_id="fixture-boot", processed_monotonic=100.,
                sensor_tick=False)


def lidar_fixture():
    lidar = object.__new__(LidarFusion)
    lidar.lock = threading.RLock()
    lidar.latest = lidar.raw_latest = lidar.scale = lidar.error = None
    lidar.config = defaults()
    lidar.processed = lidar.dropped = 0
    lidar.local_slam = SimpleNamespace(state={})
    lidar.survey_landmarks = None
    return lidar


def service_fixture():
    service = object.__new__(VisionService)
    service.lidar = lidar_fixture()
    service.stream_derivations = StreamDerivations(service)
    service.inventory = SensorInventory()
    service.map_id, service.revision, service.session_id = "fixture-map", 3, "fixture-session"
    service.calibration = {"calibration_id": "fixture-calibration"}
    service.metric_worker = SimpleNamespace(offer=lambda *_: None)
    service._stream_ahrs = lambda _frame: {"present": False, "interface": "spi"}
    service._stream_ultrasonic = lambda: {}
    service._drive_response_telemetry = lambda: {}
    return service


def wait_until(predicate, timeout=2):
    deadline = time.monotonic()+timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("fixture worker did not reach the expected boundary")
        time.sleep(.002)


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=2)
        self.path = path
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class PublicationTraceTests(unittest.TestCase):
    def setUp(self):
        self.trace = PublicationTrace(timer_factory=ManualTimer)
        self.patched = patch.object(trace_module, "TRACE", self.trace)
        self.patched.start()
        self.workers = []
        self.worker_errors = []
    def tearDown(self):
        self.trace.shutdown()
        self.patched.stop()
        for worker in self.workers:
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(self.worker_errors, [])

    def worker(self, function):
        def run():
            try:
                function()
            except BaseException as error:
                self.worker_errors.append(error)
        worker = threading.Thread(target=run, daemon=True)
        self.workers.append(worker)
        worker.start()
        return worker

    def command(self, action, epoch="fixture", **extra):
        return self.trace.command(dict(action=action, epoch=epoch,
                                       expected_pid=os.getpid(), **extra))

    def test_arm_retry_freeze_and_deadline_are_not_resets(self):
        clock = Clock()
        self.trace = PublicationTrace(clock=clock.now, cpu=clock.thread,
                                      timer_factory=ManualTimer)
        with patch.object(trace_module, "TRACE", self.trace):
            original_callbacks = list(gc.callbacks)
            first = self.command("arm", duration_s=1)
            timer = self.trace.state.timer
            clock.advance(.3)
            retry = self.command("arm", duration_s=1.)
            self.assertEqual(first["started_monotonic_s"], retry["started_monotonic_s"])
            self.assertEqual(first["deadline_monotonic_s"], retry["deadline_monotonic_s"])
            self.assertIs(timer, self.trace.state.timer)
            self.assertEqual(gc.callbacks.count(self.trace._gc_callback), 1)
            with self.assertRaisesRegex(ValueError, "parameters_mismatch"):
                self.command("arm", duration_s=2)
            with self.assertRaisesRegex(ValueError, "busy"):
                self.command("arm", epoch="second")
            with self.assertRaisesRegex(ValueError, "pid_mismatch"):
                self.trace.command(dict(action="read", epoch="fixture",
                                        expected_pid=os.getpid()+1))
            span = self.trace.span("prepare", identity("crossing"))
            span.__enter__()
            clock.advance(.8, .02)
            # Admission is stopped by nominal time even before the timer runs.
            with self.trace.span("publish", identity("too-late")):
                pass
            timer.fire()
            frozen = self.command("read")
            self.assertFalse(frozen["enabled"])
            self.assertEqual(frozen["freeze_reason"], "deadline")
            self.assertGreater(frozen["freeze_monotonic_s"], frozen["deadline_monotonic_s"])
            self.assertEqual(len(frozen["active"]), 1)
            self.assertTrue(frozen["active"][0]["incomplete"])
            self.assertIsNone(frozen["active"][0]["ended_monotonic_s"])
            self.assertIsNone(frozen["active"][0]["wall_s"])
            self.assertTrue(frozen["active"][0]["crossed_nominal_deadline"])
            with self.assertRaisesRegex(ValueError, "busy"):
                self.command("arm", epoch="new-before-finally")
            clock.advance(.2)
            span.finish()
            again = self.command("arm", duration_s=1)
            self.assertTrue(again["frozen"])
            self.assertEqual(frozen["active"], again["active"])
            self.assertEqual(frozen["freeze_monotonic_s"], again["freeze_monotonic_s"])
            self.assertEqual(gc.callbacks, original_callbacks)
            self.assertTrue(timer.cancelled)
            self.command("arm", epoch="second", duration_s=1)
            timer.fire()  # Stale timer must not freeze or remove the new callback.
            self.assertIsNone(self.trace.state.freeze_ns)
            self.assertEqual(self.trace.state.epoch, "second")
            self.assertEqual(gc.callbacks.count(self.trace._gc_callback), 1)
            self.command("freeze", epoch="second")

    def test_real_timer_expires_without_polling_and_preserves_other_callbacks(self):
        self.trace = PublicationTrace()
        previous = list(gc.callbacks)
        def other(_phase, _info):
            pass
        self.command("arm", duration_s=.03)
        gc.callbacks.append(other)
        try:
            timer = self.trace.state.timer
            timer.join(2)
            self.assertFalse(timer.is_alive())
            self.assertEqual(self.trace.state.freeze_reason, "deadline")
            self.assertNotIn(self.trace._gc_callback, gc.callbacks)
            self.assertEqual(gc.callbacks, previous+[other])
        finally:
            gc.callbacks.remove(other)

    def test_timer_creation_failure_removes_only_own_callback(self):
        def broken(*_args, **_kwargs):
            raise RuntimeError("timer fixture")
        self.trace = PublicationTrace(timer_factory=broken)
        before = list(gc.callbacks)
        with self.assertRaisesRegex(RuntimeError, "timer fixture"):
            self.command("arm")
        self.assertEqual(gc.callbacks, before)
        self.assertEqual(self.trace.state.freeze_reason, "timer_start_failed")

    def test_actual_enrich_owner_waiter_identity_and_exception_cleanup(self):
        self.command("arm")
        derivation = StreamDerivations(SimpleNamespace())
        entered, release = threading.Event(), threading.Event()
        original = derivation.orientation
        def block(metadata, baseline, reference):
            if metadata["frame_id"] == "one":
                entered.set()
                if not release.wait(2):
                    raise AssertionError("fixture barrier not released")
            return original(metadata, baseline, reference)
        derivation.orientation = block
        one, two = identity("one"), identity("two")
        self.worker(lambda: derivation.enrich(one, b"unused"))
        try:
            self.assertTrue(entered.wait(2))
            self.worker(lambda: derivation.enrich(two, b"unused"))
            def pending_two():
                return any(row["stage"] == "enrich.wait" and
                           row["identity"].get("frame_id") == "two"
                           for row in self.command("read")["active"])
            wait_until(pending_two)
            active = self.command("read")["active"]
            held = next(row for row in active if row["stage"] == "enrich.held")
            waiting = next(row for row in active if row["stage"] == "enrich.wait")
            orientation = next(row for row in active if row["stage"] == "enrich.orientation")
            self.assertEqual(held["identity"]["frame_id"], "one")
            self.assertEqual(orientation["identity"]["frame_id"], "one")
            self.assertEqual(waiting["identity"]["frame_id"], "two")
            self.assertEqual(waiting["observed_owner_token"], held["token"])
            self.assertNotEqual(waiting["ident"], held["ident"])
            self.assertNotEqual(waiting["native_id"], held["native_id"])
        finally:
            release.set()
        for thread in self.workers:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(self.command("read")["active"], [])
        with patch.object(derivation, "project", side_effect=ValueError("original app error")):
            with self.assertRaisesRegex(ValueError, "original app error"):
                derivation.enrich(identity("error"), b"")
        snapshot = self.command("freeze")
        self.assertEqual(snapshot["active"], [])
        self.assertEqual(self.trace.state.owners, {})
        self.assertEqual(snapshot["stage_stats"]["enrich.project"]["exceptions"], 1)
        self.assertEqual(snapshot["stage_stats"]["enrich.held"]["exceptions"], 1)
        self.assertEqual(snapshot["counts"]["errors"], 0)

    def test_actual_lidar_status_waiter_has_unknown_uninstrumented_owner(self):
        self.command("arm")
        lidar = lidar_fixture()
        # This is a real state lock held outside status, as enqueue/_pose may do.
        with lidar.lock:
            self.worker(lidar.status)
            wait_until(lambda: any(r["stage"] == "lidar.status.wait"
                                   for r in self.command("read")["active"]))
            active = self.command("read")["active"]
            row = next(r for r in active if r["stage"] == "lidar.status.wait")
            self.assertIsNone(row["observed_owner_token"])
            self.assertEqual(row["identity"], {})
            self.assertIn("other_holders_unknown", row["owner_scope"])
        self.workers[-1].join(2)
        self.assertFalse(self.workers[-1].is_alive())
        self.assertEqual(self.command("freeze")["stage_stats"]["lidar.status.copy"]["count"], 1)

    def test_actual_publication_bytes_and_four_lane_delivery_unchanged(self):
        image = b"\xff\xd8fixture-jpeg\xff\xd9"
        heads = [dict(id=name, valid=True, ready=True, resolution=8, sequence=17,
                      observed_monotonic_us=1234, distance_mm=list(range(64)),
                      target_status=[5]*64) for name in ("front", "rear")]
        def run(enabled):
            service = service_fixture()
            feed = DualCameraFrames(service._prepare_stream)
            rear = RearCameraFrames(feed)
            packets = []
            with feed.subscribe() as inbox:
                for camera in ("front", "rear"):
                    for sensors in (False, True):
                        metadata = identity(camera+("-tick" if sensors else "-jpeg"), camera)
                        metadata.update(type="telemetry" if sensors else "frame",
                                        sensor_tick=sensors, image_available=not sensors,
                                        sensor_packet={"schema": "biome.sensor_tick.v1",
                                                       "lidar": {"sensors": heads},
                                                       "ahrs": {"present": False, "interface": "spi"}})
                        (rear if camera == "rear" else feed).publish(
                            metadata, b"" if sensors else image)
                        value = inbox.get(timeout=1)
                        packets.append(value)
                        total, size = struct.unpack("!II", value[:8])
                        self.assertEqual(total, len(value)-4)
                        decoded = json.loads(value[8:8+size])
                        self.assertEqual(decoded["sensor_packet"]["lidar"]["sensors"], heads)
                        self.assertEqual(decoded["sensor_packet"]["ahrs"]["present"], False)
                        self.assertEqual(value[8+size:], b"" if sensors else image)
            return packets
        with patch.object(stream_derivations.time, "monotonic", return_value=100.):
            disabled = run(False)
            self.command("arm")
            enabled = run(True)
        self.assertEqual(disabled, enabled)
        snapshot = self.command("freeze")
        self.assertEqual(snapshot["stage_stats"]["prepare"]["count"], 4)
        self.assertEqual(snapshot["stage_stats"]["prepare.enrich"]["count"], 4)
        self.assertEqual(snapshot["stage_stats"]["prepare.inventory"]["count"], 4)
        self.assertEqual(snapshot["stage_stats"]["publish.serialize"]["count"], 4)
        self.assertEqual(snapshot["boundary"], "fanout_not_subscriber_write")
        self.assertEqual(snapshot["counts"]["errors"], 0)

    def test_actual_front_metadata_and_backend_start_close_spans(self):
        self.command("arm")
        service = service_fixture()
        captured = SimpleNamespace(metadata=dict(frame_id="front-fixture", source_id="front"),
                                   image=object(), decoded_monotonic=100.)
        result = service._raw_live_metadata(captured, {})
        self.assertEqual(result["frame_id"], "front-fixture")
        self.assertEqual(result["processed_monotonic"], 100.)
        self.assertEqual(result["processed_monotonic_basis"], "camera_receive_decode_time")
        backend = SimpleNamespace(start=lambda *_args, **_kwargs: None, close=lambda: None)
        service.store = SimpleNamespace(
            latest_backend_checkpoint=lambda _: {"availability": "unavailable"},
            read_map=lambda *_args, **_kwargs: {"entities": []})
        service.config = {"camera": {"expected_fps": 5}}
        service.state_dir = Path("/fixture-no-files")
        service.backend_factory = lambda *_: backend
        service._prepare_backend("map-fixture", {"calibration_id": "fixture"})
        closed = []
        backend.start = lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("backend fixture"))
        backend.close = lambda: closed.append(True)
        with self.assertRaisesRegex(ValueError, "backend fixture"):
            service._prepare_backend("map-fixture", {"calibration_id": "fixture"})
        snapshot = self.command("freeze")
        self.assertEqual(closed, [True])
        self.assertEqual(snapshot["stage_stats"]["front.raw_metadata"]["count"], 1)
        self.assertEqual(snapshot["stage_stats"]["backend.start"]["count"], 2)
        self.assertEqual(snapshot["stage_stats"]["backend.start"]["exceptions"], 1)
        self.assertEqual(snapshot["stage_stats"]["backend.close"]["count"], 1)

    def test_gc_real_callbacks_and_late_stop_are_explicit(self):
        self.command("arm")
        before = list(gc.callbacks)
        with self.trace.span("prepare", identity("gc-fixture")):
            cycle = []
            cycle.append(cycle)
            del cycle
            gc.collect(0)  # Owned test process only; never called by production trace.
        events = self.command("read")["gc_events"]
        starts = [e for e in events if e["phase"] == "start" and e["context_stage"] == "prepare"]
        self.assertTrue(starts)
        start = starts[-1]
        stop = next(e for e in events if e["phase"] == "stop" and e["sequence"] == start["sequence"])
        self.assertEqual(start["identity"]["frame_id"], "gc-fixture")
        self.assertGreaterEqual(stop["monotonic_s"], start["monotonic_s"])
        self.assertEqual(start["ident"], stop["ident"])
        self.command("freeze")
        self.assertEqual(gc.callbacks, [cb for cb in before if cb != self.trace._gc_callback])

        clock = Clock()
        late = PublicationTrace(clock=clock.now, cpu=clock.thread, timer_factory=ManualTimer)
        inputs = dict(epoch="late", expected_pid=os.getpid())
        late.command(dict(inputs, action="arm", duration_s=.1))
        try:
            late._gc_event("start", {"generation": 2})
            clock.advance(.4)
            late._gc_event("stop", {"generation": 2, "collected": 7, "uncollectable": 0})
            late.state.timer.fire()
            snapshot = late.command(dict(inputs, action="read"))
            self.assertTrue(snapshot["gc_events"][-1]["after_nominal_deadline"])
            self.assertEqual(snapshot["gc_events"][-1]["monotonic_s"], 10.4)
            self.assertIsNone(snapshot["gc_incomplete_start"])
        finally:
            late.shutdown()

    def test_identity_bounds_caps_and_thread_cpu_are_explicit(self):
        clock = Clock()
        self.trace = PublicationTrace(clock=clock.now, cpu=clock.thread,
                                      timer_factory=ManualTimer)
        class Payload:
            pass
        with patch.object(trace_module, "MAX_ACTIVE", 1), \
             patch.object(trace_module, "MAX_SLOW", 2), \
             patch.object(trace_module, "MAX_GC", 2):
            self.command("arm")
            payload = Payload()
            held = weakref.ref(payload)
            metadata = dict(identity("x"*500), pixels=payload,
                            sensor_packet={"distance_mm": [999]*64},
                            processed_monotonic=float("nan"))
            outer = self.trace.span("prepare", metadata)
            outer.__enter__()
            del payload, metadata
            gc.collect(0)
            self.assertIsNone(held())  # Trace never retains input metadata/payload.
            with self.trace.span("publish", identity("overflow")):
                pass
            self.assertEqual(self.command("read")["counts"]["active_overflow"], 1)
            clock.advance(.03, .005)
            outer.finish()
            first = self.command("read")["slow_spans"][0]
            self.assertTrue(first["identity_truncated"])
            self.assertEqual(len(first["identity"]["frame_id"]), trace_module.MAX_ID_CHARS)
            self.assertIsNone(first["identity"]["processed_monotonic"])
            for number in range(4):
                with self.trace.span("prepare", identity(str(number))):
                    clock.advance(.03, .005)
                self.trace._gc_event("start", {"generation": 0})
                self.trace._gc_event("stop", {"generation": 0, "collected": 0, "uncollectable": 0})
            snapshot = self.command("freeze")
            self.assertEqual(len(snapshot["slow_spans"]), 2)
            self.assertGreater(snapshot["counts"]["slow_overwritten"], 0)
            self.assertEqual(len(snapshot["gc_events"]), 2)
            self.assertGreater(snapshot["counts"]["gc_overwritten"], 0)
            self.assertEqual(snapshot["slow_spans"][-1]["wall_s"], .03)
            self.assertEqual(snapshot["slow_spans"][-1]["thread_cpu_s"], .005)
            self.assertLess(len(json.dumps(snapshot).encode()), trace_module.MAX_EXPORT_BYTES)
            self.assertEqual(snapshot["counts"]["errors"], 0)

    def test_depth_and_thread_bounds_do_not_leave_orphan_contexts(self):
        with patch.object(trace_module, "MAX_ACTIVE", 3), \
             patch.object(trace_module, "MAX_THREADS", 1), \
             patch.object(trace_module, "MAX_DEPTH", 1):
            self.command("arm")
            with self.trace.span("prepare", identity("parent")):
                with self.trace.span("publish", identity("depth-overflow")):
                    pass
                def other_thread():
                    with self.trace.span("publish", identity("thread-overflow")):
                        pass
                self.worker(other_thread).join(2)
                snapshot = self.command("read")
                self.assertEqual(len(snapshot["active"]), 1)
                self.assertEqual(snapshot["counts"]["depth_overflow"], 1)
                self.assertEqual(snapshot["counts"]["thread_overflow"], 1)
            self.assertEqual(self.command("freeze")["active"], [])

    def test_real_unix_operation_and_non_unix_rejection(self):
        service = object.__new__(VisionService)
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "trace.sock")
            server = LocalServer(path, handler_for(service))
            serving = threading.Thread(target=server.serve_forever, daemon=True)
            serving.start()
            try:
                connection = UnixConnection(path)
                connection.request("POST", "/v1/query", json.dumps({
                    "operation": "robot.vision.trace",
                    "inputs": dict(action="arm", epoch="fixture", expected_pid=os.getpid())}))
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                value = json.load(response)
                self.assertEqual(value["pid"], os.getpid())
                self.assertEqual(value["schema"], trace_module.SCHEMA)
                self.assertGreater(value["deadline_monotonic_s"]-value["observation_finished_monotonic_s"], 125)
                connection.close()
            finally:
                server.shutdown()
                serving.join(2)
                server.server_close()
                self.assertFalse(serving.is_alive())
        network = HTTPServer(("127.0.0.1", 0), handler_for(service))
        serving = threading.Thread(target=network.serve_forever, daemon=True)
        serving.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", network.server_port, timeout=2)
            connection.request("POST", "/v1/query", json.dumps({
                "operation": "robot.vision.trace",
                "inputs": dict(action="freeze", epoch="fixture", expected_pid=os.getpid())}))
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            self.assertEqual(json.load(response)["error"]["code"], "local_trace_only")
            self.assertIsNone(self.trace.state.freeze_ns)
            connection.close()
        finally:
            network.shutdown()
            serving.join(2)
            network.server_close()
            self.assertFalse(serving.is_alive())


if __name__ == "__main__":
    unittest.main()
