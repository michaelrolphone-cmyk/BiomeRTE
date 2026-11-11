"""Contract and persistence-boundary tests; real-engine qualification is separate."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from slam_backend import (
    BackendNotReady, CheckpointError, FORMAT, FORMAT_VERSION, PYSLAM_COMMIT,
    SlamBackend, _intrinsics_hash, _inverse_pose, _saved_entities,
)

CALIBRATION = {
    "width": 640, "height": 480, "fx": 500.0, "fy": 500.0,
    "cx": 320.0, "cy": 240.0, "distortion": [0.0] * 5,
    "calibration_id": "contract-test", "provenance": "test fixture", "validated": True,
}


def pose(x=0.0):
    result = np.eye(4)
    result[0, 3] = x
    return result


def document():
    frames = []
    for index, x in enumerate((0.0, 1.0)):
        frames.append({
            "id": index + 10, "kid": index, "img_id": index,
            "timestamp": index / 5, "pose": _inverse_pose(pose(x)).tolist(),
            "parent": 10 if index else None, "_is_bad": False,
            "des": {"fixture": "descriptor bytes are validated by the real-engine test"},
            "kps": [[1.0, 2.0]],
        })
    return {
        "map": {"keyframes": frames, "points": [
            {"id": 20, "pt": [1.0, 2.0, 3.0], "color": [10, 20, 30], "_is_bad": False}
        ]},
        "feature_tracker_config": {}, "loop_detector_config": {},
    }


class BackendContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.backend = SlamBackend({"calibration": copy.deepcopy(CALIBRATION),
                                    "mounting": {"height_m": .0635, "pitch_down_deg": 0, "validated": False}},
                                   self.root)
        self.backend._map_id = "map-one"

    def make_package(self):
        path = self.root / "package"
        path.mkdir()
        (path / "map.json").write_text(json.dumps(document()))
        (path / "config_info.json").write_text("{}")
        (path / "loop_closing.db").write_bytes(b"contract-boundary fixture")
        files = [{"path": p.name, "size": p.stat().st_size,
                  "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                 for p in sorted(path.iterdir())]
        manifest = {
            "format": FORMAT, "format_version": FORMAT_VERSION, "pyslam_commit": PYSLAM_COMMIT,
            "map_id": "map-one", "intrinsics_sha256": _intrinsics_hash(CALIBRATION),
            "files": files, "next_frame_index": 2, "source_frames": {"0": "a", "1": "b"},
        }
        (path / "backend_manifest.json").write_text(json.dumps(manifest))
        return path

    def test_mounting_alone_never_initializes_or_fabricates_geometry(self):
        backend = SlamBackend({"mounting": {"height_m": .0635, "pitch_down_deg": 0}}, self.root)
        with patch.object(backend, "_new_runtime", side_effect=AssertionError("must not import engine")):
            backend.start("observations")
            result = backend.track(np.zeros((480, 640, 3), np.uint8), "frame", 1.0)
        self.assertEqual(result["localization"]["status"], "awaiting_calibration")
        self.assertIsNone(result["localization"]["T_map_camera"])
        self.assertFalse(result["geometry"]["complete_snapshot"])
        self.assertFalse(result["geometry"]["available"])
        self.assertEqual(result["map_changes"], [])

    def test_provisional_intrinsics_are_not_reported_as_calibrated(self):
        self.backend.config["calibration"]["validated"] = False
        with patch.object(self.backend, "_new_runtime", side_effect=AssertionError):
            self.assertEqual(self.backend.start("map-one")["status"], "awaiting_calibration")

    def test_nonfinite_calibration_is_rejected_before_native_import(self):
        self.backend.config["calibration"]["fx"] = float("nan")
        with patch.object(self.backend, "_new_runtime", side_effect=AssertionError):
            self.assertEqual(self.backend.start("map-one")["status"], "awaiting_calibration")

    def test_inverse_pose_uses_camera_to_map_convention(self):
        rotation = np.array([[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]])
        transform = pose(3)
        transform[:3, :3] = rotation
        np.testing.assert_allclose(_inverse_pose(transform) @ transform, np.eye(4), atol=1e-12)

    def test_saved_geometry_comes_from_serialized_pose_and_bgr(self):
        entities = _saved_entities(document(), {"1": "source-b"})
        self.assertEqual(entities[("keyframe", "11")]["position"], [1., 0., 0.])
        self.assertEqual(entities[("keyframe", "11")]["source_frame_id"], "source-b")
        self.assertEqual(entities[("sparse_point", "20")]["rgb"], [30, 20, 10])
        self.assertEqual(entities[("keyframe", "11")]["units"], "arbitrary")

    def test_saved_keyframe_without_descriptors_is_not_a_persistent_map(self):
        broken = document()
        broken["map"]["keyframes"][0]["des"] = None
        with self.assertRaises(CheckpointError):
            _saved_entities(broken, {})

    def test_identical_geometry_produces_no_map_revision_changes(self):
        entities = _saved_entities(document(), {})
        self.backend._diff(entities)
        self.assertEqual(self.backend._diff(copy.deepcopy(entities)), [])

    def test_corrected_keyframe_keeps_identity_and_culled_point_is_deleted(self):
        entities = _saved_entities(document(), {})
        self.backend._diff(entities)
        corrected = copy.deepcopy(entities)
        corrected[("keyframe", "11")]["position"] = [2., 0., 0.]
        corrected[("keyframe", "11")]["T_map_camera"] = pose(2).tolist()
        del corrected[("sparse_point", "20")]
        changes = self.backend._diff(corrected)
        self.assertEqual({(x["op"], x["kind"], x["entity_id"]) for x in changes},
                         {("upsert", "keyframe", "11"), ("delete", "sparse_point", "20")})

    def test_complete_snapshot_reemits_all_entities_for_crash_reconciliation(self):
        entities = _saved_entities(document(), {})
        self.backend._diff(entities)
        changes = self.backend._diff(entities, complete=True)
        self.assertEqual(len(changes), len(entities))
        self.assertTrue(self.backend._geometry["complete_snapshot"])

    def test_current_pose_reapplies_corrected_reference_pose(self):
        reference = SimpleNamespace(id=10, is_bad=lambda: False, Tcw=lambda: _inverse_pose(pose(5)))
        history = SimpleNamespace(ids=[30], kf_references=[reference],
                                  relative_frame_poses=[SimpleNamespace(matrix=lambda: _inverse_pose(pose(2)))])
        self.backend._slam = SimpleNamespace(tracking=SimpleNamespace(
            f_cur=SimpleNamespace(id=30), tracking_history=history))
        transform, identifier = self.backend._current_pose()
        self.assertEqual(identifier, "10")
        np.testing.assert_allclose(transform[:3, 3], [7., 0., 0.])

    def test_stale_history_does_not_become_current_pose(self):
        history = SimpleNamespace(ids=[29])
        self.backend._slam = SimpleNamespace(tracking=SimpleNamespace(
            f_cur=SimpleNamespace(id=30), tracking_history=history))
        self.assertEqual(self.backend._current_pose(), (None, None))

    def test_corrupt_checkpoint_is_rejected_before_starting_engine(self):
        path = self.make_package()
        (path / "loop_closing.db").write_bytes(b"corrupted")
        with patch.object(self.backend, "_new_runtime", side_effect=AssertionError("must not start")):
            with self.assertRaises(CheckpointError):
                self.backend.start("map-one", {"availability": "available", "path": str(path)})

    def test_changed_intrinsics_reject_checkpoint(self):
        path = self.make_package()
        self.backend.config["calibration"]["fx"] = 510.0
        with self.assertRaisesRegex(CheckpointError, "intrinsics"):
            self.backend._validate_checkpoint(path)

    def test_restart_restores_map_but_requires_live_relocalization(self):
        path = self.make_package()
        calls = []
        runtime = SimpleNamespace(load_system_state=lambda p: calls.append(("load", p)),
                                  set_tracking_state=lambda s: calls.append(("state", s)),
                                  loop_closing=SimpleNamespace(last_loop_kf_id=0))
        self.backend._states = SimpleNamespace(INIT_RELOCALIZE="init-reloc")
        with patch.object(self.backend, "_new_runtime", return_value=runtime):
            status = self.backend.start("map-one", path)
        self.assertIn(("state", "init-reloc"), calls)
        self.assertEqual(status["status"], "relocalizing")
        self.assertFalse(status["localization"]["valid"])
        self.assertIsNone(status["localization"]["T_map_camera"])
        self.assertTrue(self.backend._complete_snapshot_pending)

    def test_gap_clears_pose_and_keeps_map_identity(self):
        calls = []
        self.backend._states = SimpleNamespace(INIT_RELOCALIZE="init-reloc")
        self.backend._slam = SimpleNamespace(
            map=SimpleNamespace(num_keyframes=lambda: 3),
            set_tracking_state=lambda s: calls.append(s),
            tracking=SimpleNamespace(cur_R=np.eye(3), cur_t=np.ones(3),
                                     motion_model=SimpleNamespace(reset=lambda: calls.append("motion-reset"))))
        self.backend._localization = {"valid": True, "T_map_camera": pose().tolist()}
        status = self.backend.invalidate_tracking("stream_reconnected")
        self.assertEqual(status["map_id"], "map-one")
        self.assertIsNone(status["localization"]["T_map_camera"])
        self.assertEqual(status["status"], "relocalizing")
        self.assertEqual(calls, ["init-reloc", "motion-reset"])

    def test_checkpoint_uses_exact_saved_geometry_not_later_live_geometry(self):
        def save(path):
            target = Path(path)
            (target / "map.json").write_text(json.dumps(document()))
            (target / "config_info.json").write_text("{}")
            (target / "loop_closing.db").write_bytes(b"boundary fixture")
        self.backend._slam = SimpleNamespace(
            map=SimpleNamespace(num_keyframes=lambda: 2, num_points=lambda: 1),
            save_system_state=save)
        self.backend._source_frames = {"1": "source-b"}
        self.backend._next_frame_index = 2
        artifact = self.backend.checkpoint(self.root / "saved")
        self.assertTrue(artifact["exact_saved_geometry"])
        self.assertTrue(artifact["geometry"]["complete_snapshot"])
        keyframe = next(x for x in artifact["map_changes"] if x["kind"] == "keyframe" and x["entity_id"] == "11")
        self.assertEqual(keyframe["data"]["position"], [1., 0., 0.])
        self.backend._validate_checkpoint(Path(artifact["path"]))

    def test_upstream_partial_save_never_gets_a_success_manifest(self):
        def save(path):
            (Path(path) / "map.json").write_text("{partial")
        self.backend._slam = SimpleNamespace(
            map=SimpleNamespace(num_keyframes=lambda: 2, num_points=lambda: 1),
            save_system_state=save)
        target = self.root / "partial"
        with self.assertRaises(json.JSONDecodeError):
            self.backend.checkpoint(target)
        self.assertFalse((target / "backend_manifest.json").exists())

    def test_empty_map_cannot_be_saved_as_success(self):
        with self.assertRaises(BackendNotReady):
            self.backend.checkpoint(self.root / "empty")

    def test_restore_counter_cannot_alias_a_saved_image(self):
        path = self.make_package()
        manifest_path = path / "backend_manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["next_frame_index"] = 1
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(CheckpointError, "next_frame_index"):
            self.backend._validate_checkpoint(path)

    def test_first_export_of_old_keyframe_is_not_current_image_evidence(self):
        entities = _saved_entities(document(), {"0": "historical"})
        native_map = SimpleNamespace(
            update_lock=threading.RLock(), get_frame=lambda index: None,
            get_keyframes=lambda: [SimpleNamespace(id=10), SimpleNamespace(id=11)])
        self.backend._slam = SimpleNamespace(
            map=native_map, track=lambda *args: None,
            tracking=SimpleNamespace(state=SimpleNamespace(name="OK")),
            loop_closing=SimpleNamespace(last_loop_kf_id=0))
        self.backend._complete_snapshot_pending = True
        with patch.object(self.backend, "_read_entities", return_value=entities), \
             patch.object(self.backend, "_current_pose", return_value=(pose(), "10")):
            result = self.backend.track(np.zeros((480, 640, 3), np.uint8), "new-input", 1.0)
        self.assertTrue(result["geometry"]["complete_snapshot"])
        self.assertTrue(result["localization"]["valid"])
        self.assertFalse(result["is_keyframe"])
        self.assertIsNone(result["observation_anchor"])

    def test_genuinely_created_native_keyframe_can_anchor_exact_input(self):
        entities = _saved_entities(document(), {})
        entities[("keyframe", "11")]["backend_frame_index"] = 0
        native_map = SimpleNamespace(update_lock=threading.RLock(), get_frame=lambda index: None,
                                     get_keyframes=lambda: [SimpleNamespace(id=10)])
        self.backend._slam = SimpleNamespace(
            map=native_map, track=lambda *args: None,
            tracking=SimpleNamespace(state=SimpleNamespace(name="OK")),
            loop_closing=SimpleNamespace(last_loop_kf_id=0))
        with patch.object(self.backend, "_read_entities", return_value=entities), \
             patch.object(self.backend, "_current_pose", return_value=(pose(), "11")):
            result = self.backend.track(np.zeros((480, 640, 3), np.uint8), "exact-input", 1.0)
        self.assertTrue(result["is_keyframe"])
        self.assertEqual(result["observation_anchor"]["keyframe_id"], "11")
        self.assertEqual(result["observation_anchor"]["source_frame_id"], "exact-input")


    def test_legacy_debug_cache_is_integrity_checked_before_restore(self):
        path = self.make_package()
        cache = path / "kf_image_map.pkl"
        cache.write_bytes(b"legacy-debug-cache-boundary-fixture")
        manifest_path = path / "backend_manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["files"].append({"path": cache.name, "size": cache.stat().st_size,
                                  "sha256": hashlib.sha256(cache.read_bytes()).hexdigest()})
        manifest_path.write_text(json.dumps(manifest))
        self.backend._validate_checkpoint(path)
        cache.write_bytes(b"corrupted")
        with patch.object(self.backend, "_new_runtime",
                          side_effect=AssertionError("must not start an engine")):
            with self.assertRaisesRegex(CheckpointError, "integrity"):
                self.backend.start("map-one", path)




    def test_normalized_restore_preserves_legacy_descriptor_history_until_new_keyframe(self):
        path = self.make_package()
        policy = {"mode":"percentile_gray","version":1,"low_percentile":1.0,"high_percentile":99.0}
        backend = SlamBackend({"calibration":copy.deepcopy(CALIBRATION),"preprocessing":policy},self.root)
        backend._states = SimpleNamespace(INIT_RELOCALIZE="init-reloc")
        native_map = SimpleNamespace(update_lock=threading.RLock(), get_frame=lambda index: None,
            get_keyframes=lambda:[SimpleNamespace(id=10),SimpleNamespace(id=11)],
            num_keyframes=lambda:3,num_points=lambda:1)
        received=[]
        def save(target):
            doc=document()
            extra=copy.deepcopy(doc["map"]["keyframes"][-1])
            extra.update(id=12,kid=2,img_id=2)
            doc["map"]["keyframes"].append(extra)
            (Path(target)/"map.json").write_text(json.dumps(doc))
            (Path(target)/"config_info.json").write_text("{}")
            (Path(target)/"loop_closing.db").write_bytes(b"fixture")
        runtime=SimpleNamespace(map=native_map,load_system_state=lambda p:None,
            set_tracking_state=lambda s:None,loop_closing=SimpleNamespace(last_loop_kf_id=0),
            tracking=SimpleNamespace(state=SimpleNamespace(name="OK")),
            track=lambda image,*args:received.append(image),save_system_state=save)
        with patch.object(backend,"_new_runtime",return_value=runtime):
            status=backend.start("map-one",path)
        raw_policy={"mode":"none","version":1}
        self.assertEqual(status["preprocessing"]["loaded"],raw_policy)
        self.assertEqual(status["preprocessing"]["descriptor_history"],[raw_policy])
        self.assertEqual(status["preprocessing"]["active"],policy)
        entities=_saved_entities(document(),{"0":"a","1":"b"})
        added=copy.deepcopy(entities[("keyframe","11")])
        added["backend_frame_index"]=2
        entities[("keyframe","12")]=added
        image=np.full((480,640,3),24,np.uint8)
        with patch.object(backend,"_read_entities",return_value=entities), \
             patch.object(backend,"_current_pose",return_value=(pose(),"12")):
            result=backend.track(image,"fresh",1.0)
        self.assertTrue(result["is_keyframe"])
        self.assertFalse(np.shares_memory(received[0],image))
        saved=backend.checkpoint(self.root/"normalized-save")
        self.assertEqual(saved["active_preprocessing"],policy)
        self.assertEqual(saved["descriptor_preprocessing_history"],[raw_policy,policy])
        backend._validate_checkpoint(Path(saved["path"]))

    def test_unsupported_checkpoint_preprocessing_is_rejected_before_native_load(self):
        path=self.make_package()
        target=path/"backend_manifest.json"
        original=json.loads(target.read_text())
        for change in [{"active_preprocessing":{"mode":"invented"}},
                       {"active_preprocessing":None},
                       {"descriptor_preprocessing_history":[None]},
                       {"descriptor_preprocessing_history":[]}]:
            with self.subTest(change=change):
                target.write_text(json.dumps({**original,**change}))
                with patch.object(self.backend,"_new_runtime",side_effect=AssertionError):
                    with self.assertRaisesRegex(CheckpointError,"preprocessing"):
                        self.backend.start("map-one",path)


if __name__ == "__main__":
    unittest.main()

