"""Full status remains a current read during map writes and checkpoint cleanup."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
import sys
import threading
import traceback
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_vision_service as fixtures


class StatusCheckpointConcurrencyTests(unittest.TestCase):
    setUp = fixtures.VisionServiceTests.setUp
    tearDown = fixtures.VisionServiceTests.tearDown
    make_service = fixtures.VisionServiceTests.make_service
    frame = fixtures.VisionServiceTests.frame
    process = fixtures.VisionServiceTests.process

    def populated_service(self):
        service = self.make_service()
        self.process(service, self.frame(service, "retained"))
        source = self.root / "backend-state.bin"
        source.write_bytes(b"independent-native-recovery-state")
        saved = [
            service.store.checkpoint(service.map_id, source, {
                "checkpoint_origin": "automatic",
                "database_mode": "revision_reference",
                "calibration_id": service.calibration["calibration_id"],
            })
            for _ in range(3)
        ]
        return service, saved

    def query_status(self, clients, service):
        future = clients.submit(service.query, "robot.vision.status", {})
        try:
            return future.result(1)
        except TimeoutError:
            frames = sys._current_frames()
            stacks = ["".join(traceback.format_stack(frames[thread.ident]))
                      for thread in threading.enumerate()
                      if thread.name.startswith("status-client") and thread.ident in frames]
            self.fail("Full status blocked:\n" + "\n".join(stacks))

    def assert_current_status(self, status, service, checkpoint):
        self.assertEqual(status["availability"], "available")
        self.assertEqual(status["map_id"], service.map_id)
        self.assertEqual(status["camera"]["state"], "synthetic_fixture")
        self.assertEqual(status["processed_frames"], service._frames)
        self.assertEqual(status["latest_checkpoint"]["checkpoint_id"],
                         checkpoint["checkpoint_id"])
        self.assertEqual(status["latest_backend_checkpoint"]["checkpoint_id"],
                         checkpoint["checkpoint_id"])
        self.assertEqual(status["latest_backend_checkpoint"]["availability"], "available")
        self.assertEqual(status["persistence"]["counts"]["checkpoints"], 3)

    def test_full_status_during_checkpoint_retirement_verifies_current_package(self):
        service, saved = self.populated_service()
        entered, release = threading.Event(), threading.Event()
        original = service.store._verified_checkpoint

        def paused_retirement(record):
            if threading.current_thread().name.startswith("checkpoint-retirement"):
                entered.set()
                if not release.wait(5):
                    raise AssertionError("retirement was never released")
            return original(record)

        with patch.object(service.store, "_verified_checkpoint", paused_retirement), \
             ThreadPoolExecutor(1, thread_name_prefix="checkpoint-retirement") as cleanup, \
             ThreadPoolExecutor(1, thread_name_prefix="status-client") as clients:
            retirement = cleanup.submit(service.store.retire_superseded_checkpoints,
                                        map_id=service.map_id)
            try:
                self.assertTrue(entered.wait(2))
                status = self.query_status(clients, service)
                self.assert_current_status(status, service, saved[-1])
                self.assertFalse(retirement.done())
            finally:
                release.set()
            retired = retirement.result(3)
        self.assertEqual(retired["retired"], [saved[0]["checkpoint_id"]])
        self.assertEqual(service.store.get_checkpoint(saved[0]["checkpoint_id"])["availability"],
                         "unavailable")

    def test_full_status_reads_committed_state_during_actual_map_transaction(self):
        service, saved = self.populated_service()
        entered, release = threading.Event(), threading.Event()
        revision = service.revision

        def pause_insert(statement):
            if statement.startswith("INSERT INTO entity_versions"):
                entered.set()
                if not release.wait(5):
                    raise AssertionError("map transaction was never released")

        service.store._db.set_trace_callback(pause_insert)
        with ThreadPoolExecutor(1, thread_name_prefix="map-writer") as mapping, \
             ThreadPoolExecutor(1, thread_name_prefix="status-client") as clients:
            write = mapping.submit(service.store.apply_map_update, service.map_id,
                                   revision, [{
                                       "op": "upsert", "kind": "sparse_point",
                                       "entity_id": "after-uncommitted-write",
                                       "data": {"xyz": [4, 5, 6]},
                                   }])
            try:
                self.assertTrue(entered.wait(2))
                status = self.query_status(clients, service)
                self.assert_current_status(status, service, saved[-1])
                current = next(item for item in status["persistence"]["maps"]
                               if item["map_id"] == service.map_id)
                self.assertEqual(current["revision"], revision)
                # Applied metric references resolve through these same read paths.
                for resolve in (service.metric.repo.resolve_keyframes,
                                service.metric.repo.resolve_keyframes_for_fit):
                    resolved = clients.submit(resolve, service.map_id, [], revision).result(1)
                    self.assertEqual(resolved[0]["revision"], revision)
                self.assertFalse(write.done())
            finally:
                release.set()
                committed = write.result(3)
                service.store._db.set_trace_callback(None)
                service.revision = committed["revision"]
        status = service.query("robot.vision.status", {})
        current = next(item for item in status["persistence"]["maps"]
                       if item["map_id"] == service.map_id)
        self.assertEqual(current["revision"], revision + 1)
        self.assertEqual(status["map_revision"], revision + 1)


    def checkpoint_read_during_retirement(self, keep_files):
        service, saved = self.populated_service()
        target = saved[0]
        entered, release = threading.Event(), threading.Event()
        original = service.store._verified_checkpoint

        def paused_reader(record):
            reading_target = (threading.current_thread().name.startswith("checkpoint-reader")
                              and record["checkpoint_id"] == target["checkpoint_id"])
            if reading_target and not keep_files:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("checkpoint read was never released")
            result = original(record)
            if reading_target and keep_files:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("checkpoint read was never released")
            return result

        removal = (patch("vision_store.shutil.rmtree", side_effect=OSError("busy package"))
                   if keep_files else nullcontext())
        with patch.object(service.store, "_verified_checkpoint", paused_reader), \
             ThreadPoolExecutor(1, thread_name_prefix="checkpoint-reader") as clients:
            read = clients.submit(service.store.get_checkpoint, target["checkpoint_id"])
            try:
                self.assertTrue(entered.wait(2))
                with removal:
                    retired = service.store.retire_superseded_checkpoints(map_id=service.map_id)
                self.assertIn(target["checkpoint_id"], retired["retired"])
            finally:
                release.set()
            result = read.result(2)
        self.assertEqual(result["availability"], "unavailable")
        self.assertEqual(Path(target["backend_artifact"]["path"]).exists(), keep_files)
        self.assertEqual(service.store.latest_backend_checkpoint(service.map_id)["checkpoint_id"],
                         saved[-1]["checkpoint_id"])

    def test_checkpoint_read_rejects_retired_inventory_even_when_files_remain(self):
        self.checkpoint_read_during_retirement(keep_files=True)

    def test_checkpoint_read_handles_package_removed_during_verification(self):
        self.checkpoint_read_during_retirement(keep_files=False)


    def test_full_status_survives_folder_retired_during_inventory(self):
        service, saved = self.populated_service()
        target = service.store._checkpoint_folder(saved[0]["checkpoint_id"])
        entered, release = threading.Event(), threading.Event()
        original = Path._scandir

        def paused_scan(path):
            if (path == target and
                    threading.current_thread().name.startswith("status-client")):
                entered.set()
                if not release.wait(5):
                    raise AssertionError("inventory scan was never released")
            return original(path)

        with patch.object(Path, "_scandir", paused_scan), \
             ThreadPoolExecutor(1, thread_name_prefix="status-client") as clients:
            query = clients.submit(service.query, "robot.vision.status", {})
            try:
                self.assertTrue(entered.wait(2))
                retired = service.store.retire_superseded_checkpoints(map_id=service.map_id)
                self.assertIn(saved[0]["checkpoint_id"], retired["retired"])
                self.assertFalse(target.exists())
            finally:
                release.set()
            status = query.result(2)
        self.assert_current_status(status, service, saved[-1])


if __name__ == "__main__":
    unittest.main()
