import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vision_store import VisionStore

class CheckpointReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = VisionStore(Path(self.temp.name) / "state")
        self.addCleanup(self.store.close)
        self.source = Path(self.temp.name) / "native.bin"
        self.source.write_bytes(b"native-map-state")

    def save(self, map_id, reference=True):
        metadata = {"checkpoint_origin":"automatic"}
        if reference: metadata["database_mode"] = "revision_reference"
        return self.store.checkpoint(map_id, self.source, metadata)

    def test_many_maps_never_copy_global_database_and_reopen(self):
        saved = []
        with patch.object(self.store, "_backup_database", side_effect=AssertionError("full copy")):
            for index in range(12):
                m = self.store.create_map({"name": str(index)})["map_id"]
                saved.append(self.save(m))
        self.assertFalse(list(self.store.checkpoint_dir.glob("*/vision.sqlite3")))
        self.store.close()
        self.store = VisionStore(Path(self.temp.name) / "state")
        self.addCleanup(self.store.close)
        for cp in saved:
            self.assertEqual(self.store.latest_backend_checkpoint(cp["map_id"])["checkpoint_id"], cp["checkpoint_id"])

    def test_revision_reference_survives_later_updates_and_detects_bad_artifact(self):
        m = self.store.create_map({})["map_id"]
        cp = self.save(m)
        self.store.apply_map_update(m, 0, [{"op":"upsert", "kind":"landmark", "entity_id":"p", "data":{"x":1}}])
        self.assertEqual(self.store.get_checkpoint(cp["checkpoint_id"])["availability"], "available")
        Path(cp["backend_artifact"]["path"]).write_bytes(b"corrupt")
        self.assertEqual(self.store.get_checkpoint(cp["checkpoint_id"])["availability"], "unavailable")

    def test_global_full_backup_conversion_preserves_all_map_recovery(self):
        saved = [self.save(self.store.create_map({})["map_id"], False) for _ in range(9)]
        result = self.store.compact_checkpoint_databases(keep=5)
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["converted"]), 4)
        self.assertEqual(len(list(self.store.checkpoint_dir.glob("*/vision.sqlite3"))), 5)
        for cp in saved:
            self.assertEqual(self.store.get_checkpoint(cp["checkpoint_id"])["availability"], "available")
        self.assertEqual(self.store.compact_checkpoint_databases(keep=5)["converted"], [])

    def test_periodic_full_backup_is_global_not_per_map(self):
        for index in range(10):
            m = self.store.create_map({})["map_id"]
            self.store.checkpoint(m, self.source, {"database_mode":"revision_reference",
                "database_backup_interval_s":3600, "checkpoint_origin":"automatic"})
        self.assertEqual(len(list(self.store.checkpoint_dir.glob("*/vision.sqlite3"))), 1)

    def test_full_backup_cap_applies_across_different_maps(self):
        saved = []
        for index in range(9):
            m = self.store.create_map({})["map_id"]
            saved.append(self.store.checkpoint(m, self.source, {
                "database_mode":"revision_reference", "database_backup_interval_s":0,
                "checkpoint_origin":"automatic"}))
            self.store.retire_superseded_checkpoints()
        self.assertEqual(len(list(self.store.checkpoint_dir.glob("*/vision.sqlite3"))), 5)
        for cp in saved:
            self.assertEqual(self.store.latest_backend_checkpoint(cp["map_id"])["availability"], "available")

    def test_close_discards_reference_ticket_without_reader(self):
        m = self.store.create_map({})["map_id"]
        self.store.prepare_checkpoint(m, {"database_mode":"revision_reference"})
        self.store.close()

    def test_native_manifest_verification_after_conversion_and_capture(self):
        for index in range(7):
            m = self.store.create_map({})["map_id"]
            native = Path(self.temp.name) / m
            native.mkdir()
            (native / "backend_manifest.json").write_text(json.dumps({"map_id":m}))
            self.store.checkpoint(m, native, {"checkpoint_origin":"automatic"})
        result = self.store.compact_checkpoint_databases(keep=5)
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["converted"]), 2)
        cp = self.store.checkpoint(m, native, {"database_mode":"revision_reference"})
        self.assertEqual(self.store.get_checkpoint(cp["checkpoint_id"])["availability"], "available")

    def test_failed_conversion_restores_original_package(self):
        saved = [self.save(self.store.create_map({})["map_id"], False) for _ in range(2)]
        original = self.store._verified_checkpoint
        def fail_reference(value):
            if value.get("publication") == "revision_reference_v3": return {"availability":"unavailable"}
            return original(value)
        with patch.object(self.store, "_verified_checkpoint", side_effect=fail_reference):
            result = self.store.compact_checkpoint_databases(keep=1)
        self.assertEqual(len(result["errors"]), 1)
        for cp in saved:
            self.assertTrue(Path(cp["database_path"]).is_file())
            self.assertEqual(self.store.get_checkpoint(cp["checkpoint_id"])["availability"], "available")

if __name__ == "__main__": unittest.main()
