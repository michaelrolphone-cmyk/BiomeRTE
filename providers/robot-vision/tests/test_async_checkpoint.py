"""Adversarial coverage of the split capture/publication boundary."""
import copy
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_vision_service as fixtures


class AsyncCheckpointTests(unittest.TestCase):
    setUp = fixtures.VisionServiceTests.setUp
    tearDown = fixtures.VisionServiceTests.tearDown
    make_service = fixtures.VisionServiceTests.make_service
    frame = fixtures.VisionServiceTests.frame
    process = fixtures.VisionServiceTests.process

    def asynchronous_backend(self, service, fail=False):
        entered, release = threading.Event(), threading.Event()
        original = service.backend.checkpoint
        def capture(target):
            value = original(target)
            return dict(geometry=value.get("geometry", {}),
                        map_changes=value.get("map_changes", []), artifact=value)
        def write(ticket):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test did not release writer")
            if fail:
                raise OSError("injected write failure")
            return ticket["artifact"]
        service.backend.capture_checkpoint = capture
        service.backend.write_checkpoint = write
        self.addCleanup(release.set)
        return entered, release

    def test_frames_and_current_map_continue_while_disk_writer_is_blocked(self):
        service = self.make_service()
        self.process(service, self.frame(service, "before"))
        entered, release = self.asynchronous_backend(service)
        future = service._schedule_checkpoint()
        self.assertTrue(entered.wait(1))
        frozen_revision = service.revision
        self.assertIs(future, service._schedule_checkpoint())
        completed = threading.Event()
        def deliver():
            self.process(service, self.frame(service, "during"))
            completed.set()
        delivery = threading.Thread(target=deliver)
        delivery.start()
        self.assertTrue(completed.wait(1), "Checkpoint writer blocked frame delivery")
        delivery.join()
        self.assertFalse(future.done())
        later = service.store.apply_map_update(service.map_id, service.revision, [
            {"op":"upsert", "kind":"sparse_point", "entity_id":"new-after-capture",
             "data":{"xyz":[4,5,6]}}])["revision"]
        service.revision = later
        release.set()
        saved = future.result(2)
        self.assertEqual(saved["map_revision"], frozen_revision)
        self.assertEqual(service._last_checkpoint_revision, frozen_revision)
        self.assertGreater(service.revision, frozen_revision)
        self.assertTrue(any(x["entity_id"] == "new-after-capture" for x in
                            service.store.read_map(service.map_id)["entities"]))
        self.assertTrue(Path(saved["database_path"]).is_file())
        self.assertEqual(service.store.get_checkpoint(saved["checkpoint_id"])["availability"], "available")

    def test_failed_background_save_retains_last_usable_checkpoint_and_frames(self):
        service = self.make_service()
        self.process(service, self.frame(service, "before"))
        previous = service._checkpoint(origin="explicit")
        entered, release = self.asynchronous_backend(service, fail=True)
        future = service._schedule_checkpoint()
        self.assertTrue(entered.wait(1))
        self.process(service, self.frame(service, "during"))
        release.set()
        with self.assertRaises(OSError):
            future.result(2)
        self.assertEqual(service._checkpoint_error, "checkpoint_failed:OSError")
        self.assertEqual(service.store.get_checkpoint(previous["checkpoint_id"])["availability"], "available")
        self.assertEqual(list(service.store.checkpoint_dir.glob("*.partial")), [])
        self.process(service, self.frame(service, "after"))

    def test_artifact_copy_never_holds_live_store_lock(self):
        service = self.make_service()
        self.process(service, self.frame(service))
        target = service.state_dir / "fixture-backend"
        artifact = service.backend.checkpoint(target)
        prepared = service.store.prepare_checkpoint(service.map_id,
                                                    {"map_revision":service.revision})
        entered, release = threading.Event(), threading.Event()
        import vision_store
        original = vision_store.shutil.copytree
        def slow_copy(*args, **kwargs):
            entered.set()
            if not release.wait(5):raise AssertionError("copy not released")
            return original(*args, **kwargs)
        outcome = []
        with patch.object(vision_store.shutil, "copytree", slow_copy):
            writer = threading.Thread(target=lambda: outcome.append(
                service.store.finish_checkpoint(prepared, artifact["path"])))
            writer.start()
            self.assertTrue(entered.wait(1))
            delivered = threading.Event()
            work = threading.Thread(target=lambda: (
                self.process(service, self.frame(service, "while-copying")), delivered.set()))
            work.start()
            try:
                self.assertTrue(delivered.wait(1), "Artifact copy held the store mutex")
            finally:
                release.set()
                writer.join(3)
                work.join(3)
        self.assertEqual(len(outcome), 1)

    def test_explicit_checkpoint_waits_without_holding_pipeline_mutex(self):
        service = self.make_service()
        self.process(service, self.frame(service))
        entered, release = self.asynchronous_backend(service)
        result = []
        request = threading.Thread(target=lambda: result.append(
            service.query("robot.map.checkpoint")))
        request.start()
        self.assertTrue(entered.wait(1))
        delivered = threading.Event()
        delivery = threading.Thread(target=lambda: (
            self.process(service, self.frame(service, "during-explicit-save")), delivered.set()))
        delivery.start()
        try:
            self.assertTrue(delivered.wait(1))
            self.assertTrue(request.is_alive())
        finally:
            release.set()
            delivery.join(3)
            request.join(3)
        self.assertEqual(len(result), 1)


if __name__ == "__main__":
    unittest.main()
