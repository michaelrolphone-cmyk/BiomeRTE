"""Checkpoint copies must preserve the admitted revision without blocking writers."""
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from vision_store import VisionStore

class AsyncSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = VisionStore(self.tmp.name)
        self.map_id = self.store.create_map()["map_id"]

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def change(self, revision, name):
        return self.store.apply_map_update(self.map_id, revision,
            [{"op":"upsert", "kind":"landmark", "entity_id":name,
              "data":{"position":[1,2,3]}}])

    def test_pinned_snapshot_excludes_later_writes_from_another_connection(self):
        self.change(0, "before")
        prepared = self.store.prepare_checkpoint(self.map_id)
        self.assertFalse((self.store.checkpoint_dir /
                          (prepared["checkpoint_id"] + ".partial") / "vision.sqlite3").exists())
        other = VisionStore(self.tmp.name)
        try:
            other.apply_map_update(self.map_id, 1,
                [{"op":"upsert","kind":"landmark","entity_id":"after","data":{}}])
        finally:
            other.close()
        # Tickets remain serializable: live SQLite handles stay private.
        import json
        json.dumps(prepared)
        result = self.store.finish_checkpoint(prepared)
        with sqlite3.connect(result["database_path"]) as db:
            self.assertEqual(db.execute("SELECT revision FROM maps WHERE id=?",
                                        (self.map_id,)).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT entity_id FROM entity_versions").fetchall(),
                             [("before",)])
        self.assertEqual(self.store.read_map(self.map_id)["map"]["revision"], 2)
        self.assertEqual(self.store._checkpoint_readers, {})

    def test_backup_worker_does_not_hold_live_store_lock(self):
        prepared = self.store.prepare_checkpoint(self.map_id)
        copying = threading.Event()
        release = threading.Event()
        errors = []
        original = self.store._backup_database
        def delayed(*args, **kwargs):
            copying.set()
            if not release.wait(5):
                raise AssertionError("test did not release backup")
            return original(*args, **kwargs)
        def finish():
            try:
                self.store.finish_checkpoint(prepared)
            except BaseException as exc:
                errors.append(exc)
        with patch.object(self.store, "_backup_database", delayed):
            worker = threading.Thread(target=finish)
            worker.start()
            self.assertTrue(copying.wait(5))
            # This must complete while the background copy is still blocked.
            done = threading.Event()
            def write():
                try:
                    self.change(0, "during")
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    done.set()
            writer = threading.Thread(target=write)
            writer.start()
            try:
                self.assertTrue(done.wait(2), "backup held live store lock")
            finally:
                release.set()
                worker.join(5)
                writer.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])

    def test_discard_and_close_release_pending_readers(self):
        prepared = self.store.prepare_checkpoint(self.map_id)
        reader = self.store._checkpoint_readers[prepared["checkpoint_id"]]
        self.store.discard_checkpoint(prepared)
        self.assertEqual(self.store._checkpoint_readers, {})
        with self.assertRaises(sqlite3.ProgrammingError):
            reader.execute("SELECT 1")
        prepared = self.store.prepare_checkpoint(self.map_id)
        reader = self.store._checkpoint_readers[prepared["checkpoint_id"]]
        self.store.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            reader.execute("SELECT 1")

    def test_failed_copy_releases_reader_and_partial_package(self):
        prepared = self.store.prepare_checkpoint(self.map_id)
        reader = self.store._checkpoint_readers[prepared["checkpoint_id"]]
        with patch.object(self.store, "_backup_database", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.store.finish_checkpoint(prepared)
        self.assertEqual(self.store._checkpoint_readers, {})
        with self.assertRaises(sqlite3.ProgrammingError):
            reader.execute("SELECT 1")
        self.assertFalse((self.store.checkpoint_dir /
                          (prepared["checkpoint_id"] + ".partial")).exists())


class MapperTimeoutTests(unittest.TestCase):
    def test_callback_timeout_is_propagated_instead_of_retried(self):
        from types import SimpleNamespace
        from mapper_checkpoint import capture_on_mapper
        class Mapper:
            def pop_keyframe(self):
                pass
        Mapper.pop_keyframe._biome_owner_capture = True
        mapper = Mapper()
        mapper.is_running = True
        mapper.stop_requested = mapper.stopped = False
        mapper.stop_mutex = threading.Lock()
        mapper.queue_condition = threading.Condition()
        mapper.queue = SimpleNamespace(put=lambda value:
            value[1].future.set_exception(TimeoutError("capture callback failed")))
        with self.assertRaisesRegex(TimeoutError, "capture callback failed"):
            capture_on_mapper(mapper, lambda: None)

class RetentionIndexTests(unittest.TestCase):
    def test_both_observation_cascade_paths_are_indexed(self):
        with tempfile.TemporaryDirectory() as folder:
            store = VisionStore(folder)
            try:
                for column, index in [("frame_id", "observations_frame"),
                                      ("snapshot_id", "observations_snapshot")]:
                    plan = store._db.execute("EXPLAIN QUERY PLAN DELETE FROM observations "
                                             "WHERE " + column + "=?", ("absent",)).fetchall()
                    self.assertTrue(any(index in row[3] for row in plan), plan)
            finally:
                store.close()

if __name__ == "__main__":
    unittest.main()
