"""Regressions for retained-map candidates behind culled DBoW3 entries.

The pure query tests execute the exact method installed by setup_slam. Set
PYSLAM_TEST_SOURCE to the pinned checkout to also verify native transforms and
the task / relocalizer wiring, without importing optional SLAM engines.
"""
import ast
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from setup_slam import PATCHES, PYSLAM_COMMIT, RELOCALIZATION_QUERY_METHOD


def query_class():
    namespace = {"LoopDetectorBase": SimpleNamespace(print=lambda *args: None),
                 "time": time}
    exec("class CandidateQuery:\n" + RELOCALIZATION_QUERY_METHOD, namespace)
    return namespace["CandidateQuery"]


class RankedDatabase:
    def __init__(self, results, size=None):
        self.results = results
        self.database_size = len(results) if size is None else size
        self.limits = []

    def size(self):
        return self.database_size

    def query(self, descriptor, max_results):
        self.limits.append(max_results)
        return self.results[:max_results]


class RelocalizationCandidateTests(unittest.TestCase):
    def detector(self, frame_ids, size=None):
        detector = query_class()()
        # Entry IDs intentionally differ from native Frame.id values.
        rows = [SimpleNamespace(id=10000+i, score=1.0-i/1000)
                for i in range(len(frame_ids))]
        detector.db = RankedDatabase(rows, size)
        detector.map_entry_id_to_frame_id = {
            row.id: frame_id for row, frame_id in zip(rows, frame_ids)
            if frame_id is not None
        }
        detector.db_query = lambda des, frame_id, count: detector.db.query(
            des, max_results=count+1)
        return detector

    def test_culled_top_six_do_not_starve_retained_keyframes(self):
        retained = [101, 203, 307, 409, 503, 607]
        detector = self.detector(list(range(900, 918)) + retained)
        rows = detector.db_query_relocalization("bow", 8000,
                                               eligible_frame_ids=retained)
        self.assertEqual([detector.map_entry_id_to_frame_id[r.id] for r in rows],
                         retained)
        self.assertEqual(detector.db.limits, [6, 12, 24])
        self.assertEqual([r.score for r in rows],
                         [row.score for row in detector.db.results[18:]])
        self.assertEqual(detector.db.size(), 24)

    def test_budget_applies_after_membership_and_unique_native_ids(self):
        detector = self.detector([900, None, 101, 101, 203, 307,
                                  409, 503, 607, 709, 809, 909])
        rows = detector.db_query_relocalization(
            "bow", 8000, eligible_frame_ids=[101,203,307,409,503,607,709,809,909])
        self.assertEqual([detector.map_entry_id_to_frame_id[r.id] for r in rows],
                         [101,203,307,409,503,607])
        self.assertEqual(detector.db.limits, [6, 12])
        self.assertEqual(len(rows), 6)  # Existing upstream effective budget.

    def test_explicit_empty_eligibility_never_queries_unrestricted_database(self):
        detector = self.detector([101, 203])
        self.assertEqual(detector.db_query_relocalization(
            "bow", 8000, eligible_frame_ids=()), [])
        self.assertEqual(detector.db.limits, [])

    def test_legacy_unspecified_eligibility_keeps_existing_query(self):
        detector = self.detector(list(range(100, 120)))
        rows = detector.db_query_relocalization("bow", 8000)
        self.assertEqual(rows, detector.db.results[:6])
        self.assertEqual(detector.db.limits, [6])

    def test_all_stale_database_exhausts_without_inventing_candidates(self):
        detector = self.detector(list(range(900, 917)))
        self.assertEqual(detector.db_query_relocalization(
            "bow", 8000, eligible_frame_ids=(101,203)), [])
        self.assertEqual(detector.db.limits, [6, 12, 17])

    def test_sparse_query_stops_when_no_more_scored_results_exist(self):
        detector = self.detector([900, 101], size=1000)
        rows = detector.db_query_relocalization("bow", 8000,
                                               eligible_frame_ids=(101,203))
        self.assertEqual([detector.map_entry_id_to_frame_id[r.id] for r in rows],
                         [101])
        self.assertEqual(detector.db.limits, [6])

    def test_query_frame_is_not_its_own_candidate(self):
        detector = self.detector([101,203])
        rows = detector.db_query_relocalization("bow", 101,
                                               eligible_frame_ids=(101,203))
        self.assertEqual([detector.map_entry_id_to_frame_id[r.id] for r in rows],
                         [203])

    def test_empty_database_does_not_query(self):
        detector = self.detector([])
        self.assertEqual(detector.db_query_relocalization(
            "bow", 8000, eligible_frame_ids=(101,)), [])
        self.assertEqual(detector.db.limits, [])


@unittest.skipUnless(os.environ.get("PYSLAM_TEST_SOURCE"),
                     "Set PYSLAM_TEST_SOURCE to verify the pinned native wiring")
class PinnedNativeRelocalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.originals = {}
        cls.patched = {}
        for path, transform in PATCHES.items():
            source = subprocess.check_output(
                ["/usr/bin/git", "-C", os.environ["PYSLAM_TEST_SOURCE"],
                 "show", PYSLAM_COMMIT+":"+path], text=True)
            cls.originals[path] = source
            cls.patched[path] = transform(source)

    def method(self, path, class_name, method_name, namespace):
        tree = ast.parse(self.patched[path])
        cls = next(node for node in tree.body
                   if isinstance(node, ast.ClassDef) and node.name == class_name)
        method = next(node for node in cls.body
                      if isinstance(node, ast.FunctionDef) and node.name == method_name)
        exec(compile(ast.Module(body=[method], type_ignores=[]),
                     path, "exec"), namespace)
        return namespace[method_name]

    def staged_installer(self, directory):
        from setup_slam import Installer, sha_text
        native = Path(os.environ["PYSLAM_TEST_SOURCE"])
        stage = Path(directory)
        previous = json.loads((native/"biome-patches.json").read_text())
        self.assertEqual(previous["pyslam_commit"], PYSLAM_COMMIT)
        prior_entries = {item["path"]:item for item in previous["patches"]}
        for path in PATCHES:
            content = (native/path).read_text()
            if path in prior_entries:
                self.assertEqual(sha_text(content),
                                 prior_entries[path]["patched_sha256"])
            destination = stage/path
            destination.parent.mkdir(parents=True,exist_ok=True)
            destination.write_text(content)
        (stage/"biome-patches.json").write_text(json.dumps(previous))
        installer = Installer.__new__(Installer)
        installer.source = stage
        installer.record = lambda phase,artifact:None
        return installer

    def original_source(self, command, text=True):
        self.assertEqual(command[:2], ["/usr/bin/git","-C"])
        self.assertEqual(command[3], "show")
        commit,path = command[4].split(":",1)
        self.assertEqual(commit,PYSLAM_COMMIT)
        return self.originals[path]

    def test_installer_migrates_recognized_vendor_hashes_and_reapplies(self):
        from setup_slam import sha_text
        with tempfile.TemporaryDirectory() as directory:
            installer = self.staged_installer(directory)
            with patch("setup_slam.subprocess.check_output",
                       side_effect=self.original_source):
                installer.patches()
                manifest_path = installer.source/"biome-patches.json"
                first_manifest = manifest_path.read_bytes()
                manifest = json.loads(first_manifest)
                self.assertEqual(len(manifest["patches"]),len(PATCHES))
                first_files = {}
                for entry in manifest["patches"]:
                    content = (installer.source/entry["path"]).read_text()
                    self.assertEqual(content,self.patched[entry["path"]])
                    self.assertEqual(sha_text(content),entry["patched_sha256"])
                    first_files[entry["path"]] = content
                installer.patches()
                self.assertEqual(manifest_path.read_bytes(),first_manifest)
                for path,content in first_files.items():
                    self.assertEqual((installer.source/path).read_text(),content)

    def test_installer_refuses_unrecognized_vendor_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            installer = self.staged_installer(directory)
            path = installer.source/next(iter(PATCHES))
            edited = path.read_text()+"\n# unknown local edit\n"
            path.write_text(edited)
            with patch("setup_slam.subprocess.check_output",
                       side_effect=self.original_source):
                with self.assertRaisesRegex(RuntimeError,"Unrecognized vendor edits"):
                    installer.patches()
            self.assertEqual(path.read_text(),edited)

    def test_all_pinned_transforms_compile(self):
        for path, source in self.patched.items():
            if path.endswith(".py"):
                compile(source, path, "exec")

    def test_native_task_copies_eligible_frame_ids(self):
        namespace = {
            "KeyFrame": object,
            "LoopDetectorTaskType": SimpleNamespace(NONE="none"),
            "LoopDetectKeyframeData": lambda kf, img: SimpleNamespace(id=kf.id),
        }
        constructor = self.method("pyslam/loop_closing/loop_detector_base.py",
                                  "LoopDetectorTask", "__init__", namespace)
        ids = [101,203]
        task = SimpleNamespace()
        constructor(task, SimpleNamespace(id=8000), None,
                    relocalization_keyframe_ids=ids)
        ids.append(307)
        self.assertEqual(task.relocalization_keyframe_ids, (101,203))
        empty = SimpleNamespace()
        constructor(empty, SimpleNamespace(id=8000), None,
                    relocalization_keyframe_ids=())
        self.assertEqual(empty.relocalization_keyframe_ids, ())

    def test_loop_closing_snapshots_native_ids_and_preserves_geometry_dispatch(self):
        submitted = []
        geometry_calls = []
        def keyframe(frame_id, kid, img_id, bad=False, descriptors=(1,)):
            return SimpleNamespace(id=frame_id, kid=kid, img_id=img_id,
                                   des=descriptors, is_bad=lambda: bad)
        frames = [keyframe(101,1,10), keyframe(203,2,12),
                  keyframe(307,3,13,bad=True),
                  keyframe(409,4,14,descriptors=()),
                  keyframe(503,5,15,descriptors=None),
                  keyframe(8000,6,16)]
        live_map = {kf.id:kf for kf in frames}
        def make_task(frame, image, task_type, **kwargs):
            task = SimpleNamespace(**kwargs)
            submitted.append(task)
            return task
        def geometry(frame, detection, keyframes_map):
            geometry_calls.append((frame, detection, keyframes_map))
            return False
        namespace = {"Frame":object,
                     "LoopDetectorTaskType":SimpleNamespace(RELOCALIZATION="relocalize"),
                     "LoopDetectorTask":make_task,
                     "LoopClosing":SimpleNamespace(print=lambda *a:None)}
        method = self.method("pyslam/loop_closing/loop_closing.py",
                             "LoopClosing", "relocalize", namespace)
        detection = object()
        runtime = SimpleNamespace(
            map=SimpleNamespace(get_keyframes=lambda:list(frames)),
            keyframes_map=live_map,
            loop_detecting_process=SimpleNamespace(relocalize=lambda task:detection),
            relocalizer=SimpleNamespace(relocalize=geometry))
        frame = SimpleNamespace(id=8000)
        self.assertFalse(method(runtime, frame, None))
        self.assertEqual(submitted[0].relocalization_keyframe_ids, (101,203))
        self.assertEqual(geometry_calls, [(frame,detection,live_map)])

    def test_dbow3_uses_eligibility_only_for_relocalization(self):
        path = "pyslam/loop_closing/loop_detector_dbow3.py"
        tree = ast.parse(self.patched[path])
        cls = next(node for node in tree.body
                   if isinstance(node, ast.ClassDef) and node.name=="LoopDetectorDBoW3")
        task_method = next(node for node in cls.body
                           if isinstance(node, ast.FunctionDef) and node.name=="run_task")
        calls = [node for node in ast.walk(task_method)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and node.func.attr=="db_query_relocalization"]
        self.assertEqual(len(calls), 1)
        self.assertTrue(any(keyword.arg=="eligible_frame_ids"
                            for keyword in calls[0].keywords))
        # Normal loop-closure retrieval keeps its original unfiltered call.
        self.assertTrue(any(isinstance(node, ast.Call)
                            and isinstance(node.func, ast.Attribute)
                            and node.func.attr=="db_query"
                            for node in ast.walk(task_method)))

    def test_culled_candidate_is_rechecked_without_check_then_lookup_race(self):
        path = "pyslam/slam/relocalizer.py"
        tree = ast.parse(self.patched[path])
        cls = next(node for node in tree.body
                   if isinstance(node, ast.ClassDef) and node.name=="Relocalizer")
        method = next(node for node in cls.body
                      if isinstance(node, ast.FunctionDef) and node.name=="relocalize")
        body = next(node for node in method.body if isinstance(node, ast.Try)).body
        start = next(i for i,node in enumerate(body)
                     if isinstance(node, ast.Assign)
                     and any(isinstance(target,ast.Name)
                             and target.id=="reloc_candidate_kfs" for target in node.targets))
        class LiveMembership(dict):
            def __contains__(self, key):
                raise AssertionError("membership then lookup races with map culling")
        good = SimpleNamespace(is_bad=lambda:False)
        bad = SimpleNamespace(is_bad=lambda:True)
        namespace = {
            "keyframes_map":LiveMembership({101:good, 203:bad}),
            "detection_output":SimpleNamespace(candidate_idxs=[307,203,101]),
        }
        exec(compile(ast.Module(body=body[start:start+2],type_ignores=[]),
                     path,"exec"), namespace)
        self.assertEqual(namespace["reloc_candidate_kfs"],[good])


if __name__ == "__main__":
    unittest.main()
