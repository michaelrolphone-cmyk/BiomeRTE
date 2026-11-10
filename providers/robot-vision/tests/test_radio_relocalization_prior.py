"""Actual pinned LoopClosing method: radio hints preserve visual recovery."""
import ast
import copy
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from setup_slam import (
    Installer, PATCHES, PYSLAM_COMMIT, RADIO_RELOCALIZATION_PRIOR_BLOCK, sha_text,
)

LOOP_PATH = "pyslam/loop_closing/loop_closing.py"
BASE_PATH = "pyslam/loop_closing/loop_detector_base.py"
PREVIOUS_LOOP_SHA256 = "19f0c2c23f33d03606651050ddf80bdbfc92a8bcf2370f1a02bfdcefb66fdd56"


def extracted_method(source, class_name, method_name, namespace):
    cls = next(node for node in ast.parse(source).body
               if isinstance(node, ast.ClassDef) and node.name == class_name)
    method = next(node for node in cls.body
                  if isinstance(node, ast.FunctionDef) and node.name == method_name)
    exec(compile(ast.Module(body=[method], type_ignores=[]), LOOP_PATH, "exec"), namespace)
    return namespace[method_name]


class Point:
    def __init__(self):
        self.views = {}

    def get_frame_view_idx(self, frame):
        return self.views.get(frame, -1)

    def add_frame_view(self, frame, index):
        if frame in self.views:
            return False
        self.views[frame] = index
        frame.points[index] = self
        return True

    def remove_frame_view(self, frame):
        self.views.pop(frame, None)
        if frame.points is not None:
            for i, point in enumerate(frame.points):
                if point is self:
                    frame.points[i] = None


class Frame:
    def __init__(self, frame_id=8000):
        self.id = frame_id
        self.is_keyframe = False
        self.points = [Point(), None]
        self.points[0].views[self] = 0
        self.outliers = [False, True]
        self.kf_ref = object()
        self._kd = object()
        self._pose = ["original", frame_id]

    def pose(self):
        return self._pose.copy()

    def update_pose(self, pose):
        self._pose = pose.copy()


class Keyframe:
    def __init__(self, frame_id, *, kid=1, img_id=10, bad=False, descriptors=(1,), points=()):
        self.id, self.kid, self.img_id = frame_id, kid, img_id
        self.bad, self.des = bad, descriptors
        self.points = list(points)

    def is_bad(self):
        return self.bad

    def get_matched_points(self):
        return [point for point in self.points if point is not None]


@unittest.skipUnless(os.environ.get("PYSLAM_TEST_SOURCE"),
                     "Set PYSLAM_TEST_SOURCE to read the pinned native methods")
class RadioRelocalizationPriorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.originals, cls.patched = {}, {}
        for path, transform in PATCHES.items():
            source = subprocess.check_output(
                ["/usr/bin/git", "-C", os.environ["PYSLAM_TEST_SOURCE"],
                 "show", PYSLAM_COMMIT + ":" + path], text=True)
            cls.originals[path] = source
            cls.patched[path] = transform(source)
        cls.baseline = cls.patched[LOOP_PATH].replace(RADIO_RELOCALIZATION_PRIOR_BLOCK, "", 1)
        cls.baseline = cls.baseline.replace("    radio_relocalization_prior_version = 1\n", "", 1)
        if sha_text(cls.baseline) != PREVIOUS_LOOP_SHA256:
            raise AssertionError("removing the new block did not reproduce the tested prior native file")
        output_class = next(node for node in ast.parse(cls.originals[BASE_PATH]).body
                            if isinstance(node, ast.ClassDef) and node.name == "LoopDetectorOutput")
        namespace = {}
        exec(compile(ast.Module(body=[output_class], type_ignores=[]), BASE_PATH, "exec"), namespace)
        cls.Output = namespace["LoopDetectorOutput"]

    def runtime(self, *, prior=None, frames=None, geometry=None, baseline=False,
                db_error=None, after_snapshot=None):
        events = []
        frames = list([Keyframe(101), Keyframe(203, kid=2, img_id=12)] if frames is None else frames)
        live_map = {kf.id: kf for kf in frames}
        database_output = self.Output("relocalization", candidate_idxs=[203],
                                      candidate_scores=[0.9], frame_id=8000)

        def snapshot():
            events.append(("snapshot",))
            result = list(frames)
            if after_snapshot:
                after_snapshot(live_map)
            return result

        def task(frame, image, task_type, **kwargs):
            value = SimpleNamespace(frame=frame, image=image, task_type=task_type, **kwargs)
            events.append(("task", frame.id, image, task_type, kwargs))
            return value

        def database(task):
            events.append(("database", task.relocalization_keyframe_ids))
            if db_error:
                raise db_error
            return database_output

        def verify(frame, detection, keyframes):
            label = "database_geometry" if detection is database_output else "radio_geometry"
            events.append((label, tuple(detection.candidate_idxs), tuple(detection.candidate_scores)))
            if geometry:
                return geometry(frame, detection, keyframes, label)
            return False

        runtime = SimpleNamespace(
            map=SimpleNamespace(get_keyframes=snapshot),
            keyframes_map=live_map,
            loop_detecting_process=SimpleNamespace(relocalize=database),
            relocalizer=SimpleNamespace(relocalize=verify),
        )
        if prior is not None:
            runtime.relocalization_prior_frame_ids = prior
        namespace = {
            "Frame": object, "time": time,
            "LoopDetectorTaskType": SimpleNamespace(RELOCALIZATION="relocalization"),
            "LoopDetectorTask": task, "LoopDetectorOutput": self.Output,
            "LoopClosing": SimpleNamespace(print=lambda *args: None),
        }
        source = self.baseline if baseline else self.patched[LOOP_PATH]
        method = extracted_method(source, "LoopClosing", "relocalize", namespace)
        return method, runtime, events, database_output

    def state(self, frame):
        return (frame.pose(), frame.points, frame.points.copy() if frame.points is not None else None,
                frame.outliers, frame.outliers.copy() if frame.outliers is not None else None,
                frame.kf_ref, frame._kd)

    def assert_state(self, frame, expected):
        pose, points, points_copy, outliers, outliers_copy, reference, kd = expected
        self.assertEqual(frame.pose(), pose)
        self.assertIs(frame.points, points)
        self.assertEqual(frame.points, points_copy)
        self.assertIs(frame.outliers, outliers)
        self.assertEqual(frame.outliers, outliers_copy)
        self.assertIs(frame.kf_ref, reference)
        self.assertIs(frame._kd, kd)

    def test_capability_marker_is_on_the_patched_native_class(self):
        cls = next(node for node in ast.parse(self.patched[LOOP_PATH]).body
                   if isinstance(node, ast.ClassDef) and node.name == "LoopClosing")
        marker = next(node for node in cls.body
                      if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name)
                              and target.id == "radio_relocalization_prior_version"
                              for target in node.targets))
        namespace = {}
        exec(compile(ast.Module(body=[marker], type_ignores=[]), LOOP_PATH, "exec"), namespace)
        self.assertEqual(namespace["radio_relocalization_prior_version"], 1)
        self.assertNotIn("radio_relocalization_prior_version", self.baseline)

    def test_absent_and_empty_priors_keep_exact_original_dispatch_sequence(self):
        frame = Frame()
        old, runtime_old, old_events, _ = self.runtime(baseline=True)
        self.assertFalse(old(runtime_old, frame, "image"))
        for prior in (None, (), []):
            method, runtime, events, _ = self.runtime(prior=prior)
            self.assertFalse(method(runtime, frame, "image"))
            self.assertEqual(events, old_events)
            self.assertEqual(runtime.relocalization_prior_frame_ids, ())
            self.assertFalse(runtime.last_radio_relocalization["attempted"])

    def test_success_uses_native_output_and_geometry_then_skips_db(self):
        frame, new_point = Frame(), Point()

        def geometry(frame, output, keyframes, label):
            self.assertEqual(label, "radio_geometry")
            self.assertIsInstance(output, self.Output)
            self.assertEqual(output.task_type, "relocalization")
            self.assertEqual(output.frame_id, frame.id)
            self.assertEqual(output.candidate_scores, [])
            frame.update_pose(["verified-visual-pose"])
            new_point.add_frame_view(frame, 1)
            frame.outliers[:] = [False, False]
            frame.kf_ref = keyframes[101]
            return True

        method, runtime, events, _ = self.runtime(
            prior=[101], frames=[Keyframe(101, points=[new_point])], geometry=geometry)
        self.assertTrue(method(runtime, frame, None))
        self.assertEqual([event[0] for event in events], ["snapshot", "radio_geometry"])
        self.assertEqual(frame.pose(), ["verified-visual-pose"])
        self.assertIs(frame.points[1], new_point)
        self.assertEqual(new_point.get_frame_view_idx(frame), 1)
        self.assertTrue(runtime.last_radio_relocalization["geometry_accepted"])
        self.assertFalse(runtime.last_radio_relocalization["fallback_used"])
        self.assertEqual(runtime.relocalization_prior_frame_ids, ())

    def test_failed_prior_restores_frame_and_transient_views_before_full_db(self):
        frame, new_point = Frame(), Point()
        expected = self.state(frame)
        old_point = frame.points[0]

        def geometry(frame, output, keyframes, label):
            if label == "radio_geometry":
                frame.update_pose(["unaccepted"])
                new_point.add_frame_view(frame, 1)
                frame.points[1] = None  # Native outlier cleanup leaves the view binding.
                frame.outliers[:] = [True, False]
                frame.kf_ref, frame._kd = object(), object()
                return False
            self.assert_state(frame, expected)
            self.assertEqual(new_point.get_frame_view_idx(frame), -1)
            self.assertEqual(old_point.get_frame_view_idx(frame), 0)
            return True

        method, runtime, events, _ = self.runtime(
            prior=[101], frames=[Keyframe(101, points=[old_point, new_point]), Keyframe(203)],
            geometry=geometry)
        self.assertTrue(method(runtime, frame, None))
        self.assertEqual([event[0] for event in events],
                         ["snapshot", "radio_geometry", "task", "database", "database_geometry"])
        # The fallback is not narrowed to the radio candidate list.
        self.assertEqual(events[3], ("database", (101, 203)))
        self.assertFalse(runtime.last_radio_relocalization["geometry_accepted"])
        self.assertTrue(runtime.last_radio_relocalization["fallback_used"])

    def test_prior_exception_restores_even_replaced_arrays_and_falls_back(self):
        frame, new_point = Frame(), Point()
        expected = self.state(frame)

        def geometry(frame, output, keyframes, label):
            if label == "radio_geometry":
                new_point.add_frame_view(frame, 1)
                frame.points = [new_point, new_point]
                frame.outliers = [True, True]
                frame.update_pose(["exception-pose"])
                frame.kf_ref, frame._kd = object(), object()
                raise RuntimeError("fixture geometry failure")
            self.assert_state(frame, expected)
            self.assertEqual(new_point.get_frame_view_idx(frame), -1)
            return False

        method, runtime, events, _ = self.runtime(
            prior=[101], frames=[Keyframe(101, points=[new_point]), Keyframe(203)],
            geometry=geometry)
        self.assertFalse(method(runtime, frame, None))
        self.assertEqual(runtime.last_radio_relocalization["error"], "RuntimeError")
        self.assertEqual(events[-1][0], "database_geometry")

    def test_original_database_exception_is_not_swallowed(self):
        method, runtime, events, _ = self.runtime(prior=[101], db_error=LookupError("DB failure"))
        with self.assertRaisesRegex(LookupError, "DB failure"):
            method(runtime, Frame(), None)
        self.assertEqual(events[-1][0], "database")
        self.assertEqual(runtime.relocalization_prior_frame_ids, ())

    def test_ids_are_native_frame_ids_unique_live_nonbad_and_descriptor_eligible(self):
        frames = [Keyframe(101, kid=1, img_id=10), Keyframe(203, kid=2, img_id=12),
                  Keyframe(307, bad=True), Keyframe(409, descriptors=()),
                  Keyframe(503, descriptors=None), Keyframe(8000)]
        method, runtime, events, _ = self.runtime(
            prior=[1, 10, True, 101.0, 999, 307, 409, 503, 8000, 203, 101, 203],
            frames=frames)
        self.assertFalse(method(runtime, Frame(), None))
        self.assertEqual(events[1], ("radio_geometry", (203, 101), ()))
        self.assertEqual(runtime.last_radio_relocalization["candidate_frame_ids"], [203, 101])
        self.assertEqual(events[3], ("database", (101, 203)))

    def test_invalid_container_and_all_invalid_priors_use_original_flow(self):
        for prior in ("101", 101, {"id": 101}, [True, 1.0, 1, 10, 999]):
            with self.subTest(prior=prior):
                method, runtime, events, _ = self.runtime(prior=prior)
                self.assertFalse(method(runtime, Frame(), None))
                self.assertEqual([event[0] for event in events],
                                 ["snapshot", "task", "database", "database_geometry"])
                self.assertFalse(runtime.last_radio_relocalization["attempted"])

    def test_culled_between_snapshot_and_prior_is_not_dereferenced(self):
        method, runtime, events, _ = self.runtime(
            prior=[101], after_snapshot=lambda live: live.pop(101))
        self.assertFalse(method(runtime, Frame(), None))
        self.assertFalse(runtime.last_radio_relocalization["attempted"])
        self.assertEqual([event[0] for event in events],
                         ["snapshot", "task", "database", "database_geometry"])

    def test_empty_live_map_keeps_empty_visual_eligibility(self):
        method, runtime, events, _ = self.runtime(prior=[101], frames=[])
        self.assertFalse(method(runtime, Frame(), None))
        self.assertFalse(runtime.last_radio_relocalization["attempted"])
        self.assertEqual(events[2], ("database", ()))

    def test_radio_budget_is_six_eligible_candidates_and_hint_is_one_shot(self):
        frames = [Keyframe(100+i, kid=i, img_id=i+1000) for i in range(10)]
        method, runtime, events, _ = self.runtime(prior=[kf.id for kf in frames], frames=frames)
        frame = Frame()
        self.assertFalse(method(runtime, frame, None))
        self.assertEqual(events[1], ("radio_geometry", tuple(range(100, 106)), ()))
        self.assertFalse(method(runtime, frame, None))
        self.assertEqual(sum(event[0] == "radio_geometry" for event in events), 1)
        self.assertEqual(sum(event[0] == "database" for event in events), 2)

    def test_none_feature_arrays_restore_without_inventing_associations(self):
        frame = Frame()
        frame.points = frame.outliers = None

        def geometry(frame, output, keyframes, label):
            if label == "radio_geometry":
                frame.points = []
                frame.outliers = []
                return False
            self.assertIsNone(frame.points)
            self.assertIsNone(frame.outliers)
            return False

        method, runtime, _, _ = self.runtime(prior=[101], geometry=geometry)
        self.assertFalse(method(runtime, frame, None))

    def test_manifest_matches_exact_transforms_and_other_native_files_unchanged(self):
        manifest = json.loads((Path(__file__).resolve().parents[1] / "pyslam-patches.json").read_text())
        self.assertEqual(manifest["pyslam_commit"], PYSLAM_COMMIT)
        self.assertEqual({entry["path"] for entry in manifest["patches"]}, set(PATCHES))
        for entry in manifest["patches"]:
            self.assertEqual(sha_text(self.originals[entry["path"]]), entry["original_sha256"])
            self.assertEqual(sha_text(self.patched[entry["path"]]), entry["patched_sha256"])
            if entry["path"].endswith(".py"):
                compile(self.patched[entry["path"]], entry["path"], "exec")
        self.assertEqual(sha_text(self.patched["pyslam/slam/relocalizer.py"]),
                         "95045f7d2c17277e3d0c6097eda8b0e0da0303a355c739605e00d15eb4a0bd85")
        self.assertEqual(sha_text(self.patched["pyslam/loop_closing/loop_detector_dbow3.py"]),
                         "e0827d2ed9d6d97f0df8a2d3d401eebad9c9f53e5466d353322852be666404af")

    def staged_installer(self, directory):
        stage = Path(directory)
        entries = []
        for path, content in self.patched.items():
            previous = self.baseline if path == LOOP_PATH else content
            destination = stage / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(previous)
            entries.append({"path": path, "original_sha256": sha_text(self.originals[path]),
                            "patched_sha256": sha_text(previous)})
        (stage / "biome-patches.json").write_text(json.dumps(
            {"pyslam_commit": PYSLAM_COMMIT, "patches": entries}))
        installer = Installer.__new__(Installer)
        installer.source = stage
        installer.record = lambda phase, result: None
        return installer

    def original_source(self, command, text=True):
        self.assertEqual(command[0:2], ["/usr/bin/git", "-C"])
        self.assertEqual(command[3], "show")
        commit, path = command[4].split(":", 1)
        self.assertEqual(commit, PYSLAM_COMMIT)
        return self.originals[path]

    def test_recognized_installed_loop_patch_migrates_in_temp_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            installer = self.staged_installer(directory)
            self.assertEqual(sha_text((installer.source / LOOP_PATH).read_text()), PREVIOUS_LOOP_SHA256)
            with patch("setup_slam.subprocess.check_output", side_effect=self.original_source):
                installer.patches()
                first_manifest = (installer.source / "biome-patches.json").read_bytes()
                first_files = {path: (installer.source / path).read_bytes() for path in PATCHES}
                self.assertEqual((installer.source / LOOP_PATH).read_text(), self.patched[LOOP_PATH])
                installer.patches()
                self.assertEqual((installer.source / "biome-patches.json").read_bytes(), first_manifest)
                self.assertEqual({path: (installer.source / path).read_bytes() for path in PATCHES},
                                 first_files)

    def test_unknown_vendor_edit_is_preserved_and_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            installer = self.staged_installer(directory)
            target = installer.source / LOOP_PATH
            unknown = target.read_text() + "\n# unrelated local edit\n"
            target.write_text(unknown)
            with patch("setup_slam.subprocess.check_output", side_effect=self.original_source):
                with self.assertRaisesRegex(RuntimeError, "Unrecognized vendor edits"):
                    installer.patches()
            self.assertEqual(target.read_text(), unknown)


if __name__ == "__main__":
    unittest.main()
