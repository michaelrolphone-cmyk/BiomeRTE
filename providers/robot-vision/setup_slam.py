#!/usr/bin/env python3
"""Install the native Apple Silicon ORB/DBoW3 pySLAM backend in its own prefix.

No sudo, Homebrew mutation, base-conda mutation, all-model installer, or runtime
timeout is used. Each phase is restartable. Optional upstream engines retain
their implementations; their factories are imported only when selected.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

PYSLAM_COMMIT = "a4e6a31f211ac6b2f9ecf4ab628734f6194caa15"
PYSLAM_URL = "https://github.com/luigifreda/pyslam.git"
VOCABULARY_URL = "https://drive.google.com/uc?id=1-4qDFENJvswRd1c-8koqt3_5u1jMR4aF"
CONDA_PACKAGES = [
    "python=3.11", "numpy=1.26.4", "scipy", "pyyaml", "pip", "cmake<4",
    "ninja", "pybind11", "eigen=3.4", "libopencv=4.10", "py-opencv=4.10",
    "boost-cpp=1.84", "tbb", "tbb-devel", "suitesparse=7.10.1", "gtsam=4.2",
]


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha_text(value):
    return hashlib.sha256(value.encode()).hexdigest()


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise RuntimeError("Pinned upstream patch context no longer matches: " + old[:100])
    return source.replace(old, new, 1)


def patch_slam(source):
    # An annotation must not import every dense reconstruction implementation.
    source = replace_once(source,
        "from pyslam.dense.volumetric_integrator_base import VolumetricIntegrationOutput\n", "")
    source = replace_once(source,
        "def get_dense_map(self) -> VolumetricIntegrationOutput:",
        'def get_dense_map(self) -> "VolumetricIntegrationOutput":')
    source = replace_once(source,
        "from pyslam.semantics.semantic_mapping_factory import semantic_mapping_factory\n", "")
    source = replace_once(source,
        "            self.semantic_mapping = semantic_mapping_factory(",
        "            from pyslam.semantics.semantic_mapping_factory import semantic_mapping_factory\n"
        "            self.semantic_mapping = semantic_mapping_factory(")
    source = replace_once(source,
        "from pyslam.dense.volumetric_integrator_factory import volumetric_integrator_factory\n", "")
    source = replace_once(source,
        "        if Parameters.kDoVolumetricIntegration:\n",
        "        if Parameters.kDoVolumetricIntegration:\n"
        "            from pyslam.dense.volumetric_integrator_factory import volumetric_integrator_factory\n")
    # Restoring upstream state recreates subcomponents. Preserve the existing
    # runtime mode instead of silently reverting to each factory's GUI default.
    source = replace_once(source,
        "                self.init_loop_closing(loop_detector_config)\n",
        "                self.init_loop_closing(loop_detector_config, headless=self.headless)\n")
    source = replace_once(source,
        "                self.init_semantic_mapping(semantic_mapping_config)\n",
        "                self.init_semantic_mapping(semantic_mapping_config, headless=self.headless)\n")
    return replace_once(source,
        "                self.init_loop_closing(loop_detector_config, do_start_loop_closing=False)\n",
        "                self.init_loop_closing(loop_detector_config, headless=self.headless, "
        "do_start_loop_closing=False)\n")


def patch_loop_configs(source):
    imports = {
        "LoopDetectorDBoW2": "loop_detector_dbow2",
        "LoopDetectorDBoW3": "loop_detector_dbow3",
        "LoopDetectorOBIndex2": "loop_detector_obindex2",
        "LoopDetectorIBow": "loop_detector_ibow",
        "LoopDetectorHdcDelf": "loop_detector_vpr",
        "LoopDetectorEigenPlaces": "loop_detector_vpr",
        "LoopDetectorNetVLAD": "loop_detector_vpr",
        "LoopDetectorSad": "loop_detector_vpr",
        "LoopDetectorAlexNet": "loop_detector_vpr",
        "LoopDetectorCosPlace": "loop_detector_vpr",
        "LoopDetectorVlad": "loop_detector_vlad",
        "GlobalFeatureMegaloc": "global_feature_megaloc",
    }
    lines = source.splitlines(keepends=True)
    for node in reversed(ast.parse(source).body):
        if isinstance(node, ast.ImportFrom) and node.module in set(imports.values()):
            del lines[node.lineno - 1:node.end_lineno]
    source = "".join(lines)
    for name, module in imports.items():
        source = replace_once(source, "        loop_detector = " + name + "(",
                              "        from ." + module + " import " + name + "\n"
                              "        loop_detector = " + name + "(")
    return source


def patch_headless_image_cache(source):
    source = replace_once(
        source,
        "        self.store_kf_imgs = (\n",
        "        self.store_kf_imgs = not self.headless and (\n",
    )
    source = replace_once(source,
        "            if Parameters.kLoopClosingDebugWithLoopConsistencyCheckImages\n",
        "            if not self.headless and Parameters.kLoopClosingDebugWithLoopConsistencyCheckImages\n")
    for method in ("save_image_map_", "load_image_map_"):
        header = f"    def {method}(self, path):\n"
        source = replace_once(
            source, header, header + "        if self.headless:\n            return\n",
        )
    return replace_once(source,
        "        if (\n            self.loop_consistent_candidate_imgs.candidates is not None\n            or self.draw_similarity_matrix_init\n        ):",
        "        if not self.headless and (\n            (self.loop_consistent_candidate_imgs is not None\n             and self.loop_consistent_candidate_imgs.candidates is not None)\n            or self.draw_similarity_matrix_init\n        ):")


# This method is installed verbatim in the pinned DBoW3 detector. Keeping its
# source here makes the eligibility query testable without native SLAM imports.
RELOCALIZATION_QUERY_METHOD = """    def db_query_relocalization(
        self, global_des, frame_id, max_num_results=5, eligible_frame_ids=None
    ):
        # Map culling does not remove DBoW3 entries. Apply current-map membership
        # before the candidate budget, while retaining DBoW3's score ordering.
        if eligible_frame_ids is None:
            return self.db_query(global_des, frame_id, max_num_results)
        eligible = set(eligible_frame_ids)
        eligible.discard(frame_id)
        budget = max_num_results + 1  # Preserve this upstream profile's budget.
        database_size = int(self.db.size())
        if not eligible or budget <= 0 or database_size <= 0:
            return []
        limit = min(budget, database_size)
        queries = 0
        started = time.perf_counter()
        while True:
            results = self.db.query(global_des, max_results=limit)
            queries += 1
            selected = []
            seen = set()
            for result in results:
                candidate_id = self.map_entry_id_to_frame_id.get(result.id)
                if candidate_id in eligible and candidate_id not in seen:
                    selected.append(result)
                    seen.add(candidate_id)
                    if len(selected) >= budget:
                        break
            if (
                len(selected) >= min(budget, len(eligible))
                or limit >= database_size
                or len(results) < limit
            ):
                LoopDetectorBase.print(
                    f"LoopDetectorDBoW3: Relocalization eligibility: frame: {frame_id}, "
                    f"eligible: {len(eligible)}, database: {database_size}, "
                    f"query_limit: {limit}, queries: {queries}, "
                    f"elapsed_s: {time.perf_counter()-started:.6f}, "
                    f"selected: {[self.map_entry_id_to_frame_id[r.id] for r in selected]}"
                )
                return selected
            limit = min(database_size, limit * 2)

"""


def patch_loop_detector_task(source):
    source = replace_once(source, "        load_save_path=None,\n    ):\n", "        load_save_path=None,\n        relocalization_keyframe_ids=None,\n    ):\n")
    return replace_once(source, "        self.load_save_path = load_save_path\n", "        self.load_save_path = load_save_path\n        # Native Frame.id values, not keyframe kid or source image indices.\n        self.relocalization_keyframe_ids = (\n            None if relocalization_keyframe_ids is None\n            else tuple(int(frame_id) for frame_id in relocalization_keyframe_ids)\n        )\n")


def patch_loop_relocalization_context(source):
    return replace_once(source, "        task = LoopDetectorTask(\n            frame, img, task_type, covisible_keyframes=[], connected_keyframes=[]\n        )\n", "        # Snapshot under the map's lock; the relocalizer rechecks live membership.\n        eligible_keyframe_ids = tuple(\n            kf.id for kf in self.map.get_keyframes()\n            if not kf.is_bad() and kf.id != frame.id\n            and kf.des is not None and len(kf.des) > 0\n        )\n        task = LoopDetectorTask(\n            frame, img, task_type, covisible_keyframes=[], connected_keyframes=[],\n            relocalization_keyframe_ids=eligible_keyframe_ids,\n        )\n")


def patch_dbow3_relocalization(source):
    source = replace_once(
        source, "    def run_task(self, task: LoopDetectorTask):\n",
        RELOCALIZATION_QUERY_METHOD + "    def run_task(self, task: LoopDetectorTask):\n")
    return replace_once(source, "        if task.task_type == LoopDetectorTaskType.RELOCALIZATION:\n            if self.entry_id >= 1:\n                results = self.db_query(\n                    keyframe.g_des, frame_id, max_num_results=kMaxResultsForLoopClosure\n                )\n", "        if task.task_type == LoopDetectorTaskType.RELOCALIZATION:\n            if self.entry_id >= 1:\n                results = self.db_query_relocalization(\n                    keyframe.g_des, frame_id, max_num_results=kMaxResultsForLoopClosure,\n                    eligible_frame_ids=getattr(task, \"relocalization_keyframe_ids\", None),\n                )\n")


def patch_pnp_exhaustion(source):
    # Honor both the per-call slice and the solver's existing total budget.
    source = replace_once(source,
        "while (mnIterations < mRansacMaxIts || nCurrentIterations < nIterations)",
        "while (mnIterations < mRansacMaxIts && nCurrentIterations < nIterations)")
    return replace_once(source, "                Tout = mRefinedTcw;\n                return true;",
        "                Tout = mRefinedTcw;\n                bNoMore = mnIterations >= mRansacMaxIts;\n                return true;")


def patch_relocalizer(source):
    source = patch_relocalizer_membership(source)
    # The native solver may return its best pose on its final iteration.
    source = replace_once(source,
        "                        num_candidates -= 1\n                        continue\n\n                    if not ok:",
        "                        num_candidates -= 1\n\n                    if not ok:")
    # A sparse retained map can contain fewer points than the acceptance threshold.
    # The projection expansion only considers this candidate's matched map points.
    source = replace_once(source,
        "                if kf.id == frame.id or kf.is_bad():",
        "                if (kf.id == frame.id or kf.is_bad() or\n"
        "                        len(kf.get_matched_points()) < Parameters.kRelocalizationDoPoseOpt2NumInliers):")
    return source


def patch_relocalizer_membership(source):
    return replace_once(source, "            reloc_candidate_kfs = [\n                keyframes_map[idx]\n                for idx in detection_output.candidate_idxs\n                if idx in keyframes_map\n            ]  # get back the keyframes from their ids\n", "            reloc_candidate_kfs = []\n            for idx in detection_output.candidate_idxs:\n                # A keyframe can be culled while the detector process is querying.\n                candidate = keyframes_map.get(idx)\n                if candidate is not None and not candidate.is_bad():\n                    reloc_candidate_kfs.append(candidate)\n")


# Installed verbatim after the live-keyframe eligibility snapshot. No BoW score
# is invented for a radio hint, and the complete visual fallback stays intact.
RADIO_RELOCALIZATION_PRIOR_BLOCK = """        # Radio supplies candidate Frame.id values only. Every accepted pose must
        # still pass the unchanged native visual/geometric relocalizer.
        requested_radio_ids = getattr(self, "relocalization_prior_frame_ids", ())
        self.relocalization_prior_frame_ids = ()  # one attempt, never a sticky prior
        eligible_radio_ids = set(eligible_keyframe_ids)
        radio_ids = []
        if isinstance(requested_radio_ids, (tuple, list)):
            for candidate_id in requested_radio_ids:
                if (type(candidate_id) is not int or candidate_id not in eligible_radio_ids
                        or candidate_id in radio_ids):
                    continue
                candidate = self.keyframes_map.get(candidate_id)
                if candidate is None or candidate.is_bad():
                    continue
                radio_ids.append(candidate_id)
                if len(radio_ids) == 6:
                    break
        self.last_radio_relocalization = {
            "frame_id": frame.id, "candidate_frame_ids": radio_ids,
            "attempted": bool(radio_ids), "geometry_accepted": False,
            "fallback_used": True, "error": None,
        }
        if radio_ids:
            radio_started = time.perf_counter()
            radio_accepted = False
            saved_frame = None
            saved_views = {}
            try:
                points = frame.points
                outliers = frame.outliers
                saved_frame = (
                    frame.pose(), points, None if points is None else points.copy(),
                    outliers, None if outliers is None else outliers.copy(),
                    frame.kf_ref, getattr(frame, "_kd", None),
                )
                # Projection matching also adds transient MapPoint frame views.
                # Record existing bindings so a failed prior cannot prevent the
                # same visual match during the complete DBoW fallback.
                for candidate_id in radio_ids:
                    candidate = self.keyframes_map.get(candidate_id)
                    if candidate is None or candidate.is_bad():
                        continue
                    for point in candidate.get_matched_points():
                        if point is not None and point not in saved_views:
                            saved_views[point] = point.get_frame_view_idx(frame)
                radio_output = LoopDetectorOutput(
                    task_type, candidate_idxs=radio_ids, candidate_scores=[],
                    frame_id=frame.id,
                )
                radio_accepted = bool(self.relocalizer.relocalize(
                    frame, radio_output, self.keyframes_map
                ))
            except Exception as error:
                # This optional pass cannot suppress ordinary visual recovery.
                self.last_radio_relocalization["error"] = type(error).__name__
                LoopClosing.print(
                    f"Relocalization radio prior failed on frame {frame.id}: "
                    f"{type(error).__name__}"
                )
            finally:
                if not radio_accepted and saved_frame is not None:
                    try:
                        for point, previous_index in saved_views.items():
                            if previous_index < 0 and point.get_frame_view_idx(frame) >= 0:
                                point.remove_frame_view(frame)
                    except Exception as error:
                        self.last_radio_relocalization["error"] = (
                            "frame_view_restore:" + type(error).__name__
                        )
                        LoopClosing.print(
                            f"Relocalization radio frame-view cleanup failed: {type(error).__name__}"
                        )
                    finally:
                        pose, points, points_copy, outliers, outliers_copy, reference, kd = saved_frame
                        if points is not None:
                            points[:] = points_copy
                        if outliers is not None:
                            outliers[:] = outliers_copy
                        frame.points, frame.outliers, frame.kf_ref, frame._kd = points, outliers, reference, kd
                        frame.update_pose(pose)
            self.last_radio_relocalization["geometry_accepted"] = radio_accepted
            self.last_radio_relocalization["fallback_used"] = not radio_accepted
            self.last_radio_relocalization["elapsed_s"] = time.perf_counter() - radio_started
            LoopClosing.print(
                f"Relocalization radio prior: candidates={radio_ids}, "
                f"geometry_accepted={radio_accepted}, fallback={not radio_accepted}"
            )
            if radio_accepted:
                return True
"""


def patch_radio_relocalization_prior(source):
    source = replace_once(source, "class LoopClosing:\n",
        "class LoopClosing:\n    radio_relocalization_prior_version = 1\n")
    marker = "        task = LoopDetectorTask(\n            frame, img, task_type, covisible_keyframes=[], connected_keyframes=[],\n"
    return replace_once(source, marker, RADIO_RELOCALIZATION_PRIOR_BLOCK + marker)


def patch_loop_viewer(source):
    source = replace_once(source, "from pyslam.viz.qimage_thread import QimageViewer\n", "")
    source = replace_once(source,
        "        if QimageViewer.is_running():\n            QimageViewer.get_instance().quit()",
        "        if not self.headless:\n"
        "            from pyslam.viz.qimage_thread import QimageViewer\n"
        "            if QimageViewer.is_running():\n"
        "                QimageViewer.get_instance().quit()")
    source = replace_once(source,
        "        if self.headless:\n            return\n        draw = False",
        "        if self.headless:\n            return\n"
        "        from pyslam.viz.qimage_thread import QimageViewer\n"
        "        draw = False")
    return patch_radio_relocalization_prior(
        patch_loop_relocalization_context(patch_headless_image_cache(source))
    )



def patch_g2o_link(source):
    source = replace_once(source, "    opengl_helper\n", "")
    return source + "\nif(TARGET opengl_helper)\n  target_link_libraries(g2o PRIVATE opengl_helper)\nendif()\n"



def patch_gtsam_factor_imports(source):
    # The qualified profile selects g2o for tracking, BA and loop correction.
    # Keep every GTSAM algorithm intact, importing its optional native factors
    # only inside functions that use them. No replacement module is supplied.
    source = replace_once(source, "import gtsam_factors\n", "")
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    insertions = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        references = [item for item in ast.walk(node)
                      if isinstance(item, ast.Name) and item.id == "gtsam_factors"]
        if not references:
            continue
        first = node.body[0]
        docstring = (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                     and isinstance(first.value.value, str))
        position = first.end_lineno if docstring else first.lineno - 1
        insertions.append((position, " " * (node.col_offset + 4) + "import gtsam_factors\n"))
    for position, statement in sorted(set(insertions), reverse=True):
        lines.insert(position, statement)
    return "".join(lines)



def patch_empty_frame_arrays(source):
    source = replace_once(source, '                self.points = np.full(num_kps, None, dtype=object)  # init map points\n                self.outliers = np.full(num_kps, False, dtype=bool)\n\n            if self.kps_r is not None:\n', '                self.points = np.full(num_kps, None, dtype=object)  # init map points\n                self.outliers = np.full(num_kps, False, dtype=bool)\n            elif self.kps is not None:\n                # A real zero-feature observation remains serializable and reloadable.\n                self.kps = np.empty((0, 2), dtype=np.float32)\n                self.kpsu = np.empty((0, 2), dtype=np.float32)\n                self.kpsn = np.empty((0, 2), dtype=np.float32)\n                self.octaves = np.empty(0, dtype=np.uint8)\n                self.sizes = np.empty(0, dtype=np.float32)\n                self.angles = np.empty(0, dtype=np.float32)\n                self.points = np.empty(0, dtype=object)\n                self.outliers = np.empty(0, dtype=bool)\n\n            if self.kps_r is not None:\n')
    return replace_once(source, '        frame_data_dict["kpsn"] = deserialize_array_flexible(json_str["kpsn"], dtype=np.float32)\n        frame_data_dict["kps_sem"] = deserialize_array_flexible(\n', '        frame_data_dict["kpsn"] = deserialize_array_flexible(json_str["kpsn"], dtype=np.float32)\n        # JSON [] loses the second dimension of an empty XY array.\n        for name in ("kps", "kps_r", "kpsu", "kpsn"):\n            value = frame_data_dict[name]\n            if value is not None and value.size == 0:\n                frame_data_dict[name] = value.reshape(0, 2)\n        frame_data_dict["kps_sem"] = deserialize_array_flexible(\n')

def patch_tracking_continuity(source):
    old = """        need_reset = self.slam.reset_requested or (
            self.state == SlamState.LOST and self.map.num_keyframes_session() <= 5
        )"""
    new = """        # A weak tracking interval must not erase the environment or the
        # additions made since loading its checkpoint. Follow the normal
        # relocalization path used by larger maps; explicit reset still works.
        preserve_geometry = (
            getattr(self.slam, "preserve_map_on_tracking_loss", False)
            and self.map.num_keyframes() >= 2 and self.map.num_points() > 0
        )
        need_reset = self.slam.reset_requested or (
            self.state == SlamState.LOST and self.map.num_keyframes_session() <= 5
            and not preserve_geometry
        )"""
    return replace_once(source, old, new)


def patch_keyframe_graph_restore(source):
    for field in ("children", "loop_edges"):
        old = "{get_object_with_id(id, keyframes_dict) for id in self." + field + "}"
        new = "{get_object_with_id(id, keyframes_dict) for id in self." + field + " if id in keyframes_dict}"
        source = replace_once(source, old, new)
    for field in ("connected_keyframes_weights", "ordered_keyframes_weights"):
        old = "                    for id, weight in self." + field + "\n"
        source = replace_once(source, old, old + "                    if id in keyframes_dict\n")
    return source


PATCHES = {
    "pyslam/slam/keyframe.py": patch_keyframe_graph_restore,
    "pyslam/slam/tracking.py": patch_tracking_continuity,
    "pyslam/slam/frame.py": patch_empty_frame_arrays,
    "pyslam/slam/optimizer_gtsam.py": patch_gtsam_factor_imports,
    "thirdparty/g2opy/python/CMakeLists.txt": patch_g2o_link,
    "pyslam/slam/slam.py": patch_slam,
    "pyslam/loop_closing/loop_detector_configs.py": patch_loop_configs,
    "pyslam/loop_closing/loop_closing.py": patch_loop_viewer,
    "pyslam/loop_closing/loop_detector_base.py": patch_loop_detector_task,
    "pyslam/loop_closing/loop_detector_dbow3.py": patch_dbow3_relocalization,
    "pyslam/slam/relocalizer.py": patch_relocalizer,
    "cpp/modules/solvers/MLPnPsolver.cpp": patch_pnp_exhaustion,
}


class Installer:
    def __init__(self, state, conda, jobs):
        self.state = Path(state).expanduser().resolve()
        self.prefix = self.state / "env"
        self.source = self.state / "vendor/pyslam"
        self.python = self.prefix / "bin/python"
        self.conda = str(Path(conda).expanduser())
        self.jobs = jobs
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise RuntimeError("This setup profile is qualified only for native Apple Silicon macOS.")
        if self.prefix.resolve() == Path(self.conda).resolve().parent.parent:
            raise RuntimeError("Refusing to mutate the base conda environment.")
        self.state.mkdir(parents=True, exist_ok=True)
        self.logs = self.state / "setup-logs"
        self.logs.mkdir(exist_ok=True)
        self.receipt_path = self.state / "slam-installation.json"
        self.receipt = json.loads(self.receipt_path.read_text()) if self.receipt_path.exists() else {}
        self.env = os.environ.copy()
        self.env.update(
            PATH=str(self.prefix / "bin") + ":/usr/bin:/bin:/usr/sbin:/sbin",
            CONDA_PREFIX=str(self.prefix), CMAKE_PREFIX_PATH=str(self.prefix),
            PKG_CONFIG_PATH=str(self.prefix / "lib/pkgconfig"),
            PYSLAM_USE_CPP="false",
        )
        for key in ("CC", "CXX", "CFLAGS", "CXXFLAGS", "LDFLAGS", "CPPFLAGS", "SDKROOT",
                    "CONDA_BUILD_SYSROOT", "CONDA_BUILD_CROSS_COMPILATION", "KMP_DUPLICATE_LIB_OK"):
            self.env.pop(key, None)

    def run(self, argv, label, cwd=None):
        path = self.logs / (label + ".log")
        print(json.dumps({"phase": label, "log": str(path)}), flush=True)
        with path.open("a") as output:
            output.write("\n" + json.dumps([str(a) for a in argv]) + "\n")
            output.flush()
            result = subprocess.run([str(a) for a in argv], cwd=cwd, env=self.env,
                                    stdout=output, stderr=subprocess.STDOUT)
        if result.returncode:
            print(path.read_text()[-10000:], file=sys.stderr)
            raise subprocess.CalledProcessError(result.returncode, argv)
        return path

    def record(self, phase, data):
        self.receipt.update(pyslam_commit=PYSLAM_COMMIT, state_dir=str(self.state),
                            python=str(self.python), updated_at_unix_s=time.time())
        self.receipt[phase] = data
        temporary = self.receipt_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.receipt, indent=2, sort_keys=True))
        temporary.replace(self.receipt_path)

    def environment(self):
        exists = self.python.exists()
        command = [self.conda, "install" if exists else "create", "-y", "--solver", "libmamba",
                   "--override-channels", "-c", "conda-forge", "--prefix", str(self.prefix)]
        if exists:
            command.append("--freeze-installed")
        self.run(command + CONDA_PACKAGES, "environment")
        self.run([self.conda, "list", "--prefix", str(self.prefix), "--explicit"],
                 "conda-explicit")
        self.record("environment", {"packages": CONDA_PACKAGES,
                                    "explicit_log": str(self.logs / "conda-explicit.log")})

    def checkout(self):
        self.source.parent.mkdir(parents=True, exist_ok=True)
        if not self.source.exists():
            self.run(["/usr/bin/git", "clone", "--no-checkout", "--filter=blob:none",
                      PYSLAM_URL, str(self.source)], "source-clone")
            self.run(["/usr/bin/git", "-C", str(self.source), "checkout", "--detach", PYSLAM_COMMIT],
                     "source-checkout")
        revision = subprocess.check_output(
            ["/usr/bin/git", "-C", str(self.source), "rev-parse", "HEAD"], text=True).strip()
        if revision != PYSLAM_COMMIT:
            raise RuntimeError("Existing vendor checkout is not the pinned commit; it was not modified.")
        self.record("source", {"url": PYSLAM_URL, "commit": revision})

    def patches(self):
        manifest = []
        previous_path = self.source / "biome-patches.json"
        previous = json.loads(previous_path.read_text()) if previous_path.is_file() else {}
        previous_entries = {entry["path"]: entry for entry in previous.get("patches", [])}
        if previous and previous.get("pyslam_commit") != PYSLAM_COMMIT:
            raise RuntimeError("Existing patch manifest belongs to another upstream revision")
        for name, transform in PATCHES.items():
            original = subprocess.check_output(
                ["/usr/bin/git", "-C", str(self.source), "show", PYSLAM_COMMIT + ":" + name],
                text=True)
            patched = transform(original)
            if name.endswith(".py"):
                compile(patched, name, "exec")
            path = self.source / name
            current = path.read_text()
            previous_entry = previous_entries.get(name, {})
            recorded_previous = (
                previous_entry.get("original_sha256") == sha_text(original)
                and previous_entry.get("patched_sha256") == sha_text(current))
            if current not in (original, patched) and not recorded_previous:
                raise RuntimeError("Unrecognized vendor edits; leaving them intact: " + name)
            path.write_text(patched)
            manifest.append({"path": name, "original_sha256": sha_text(original),
                             "patched_sha256": sha_text(patched),
                             "reason": ("Retain initialized submap geometry through tracking loss; relocalize instead of resetting the session."
                                        if name.endswith("/tracking.py") else
                                        "Link the optional g2o OpenGL helper only when its target exists."
                                        if name.endswith("CMakeLists.txt") else
                                        "Import optional GTSAM native factors only inside functions that use them."
                                        if name.endswith("optimizer_gtsam.py") else
                                        "Keep real empty feature arrays serializable and restore their XY dimensions."
                                        if name.endswith("/frame.py") else
                                        "Keep optional dense/GUI imports lazy and preserve headless mode on restore."
                                        if name.endswith("/slam.py") else
                                        "Keep headless checkpoints small, filter live relocalization candidates, and try radio candidate hints through unchanged geometry before full visual fallback."
                                        if name.endswith("/loop_closing.py") else
                                        "Carry an immutable snapshot of eligible native keyframe IDs to the loop detector process."
                                         if name.endswith("/loop_detector_base.py") else
                                         "Apply retained-map membership before the ranked relocalization candidate budget."
                                         if name.endswith("/loop_detector_dbow3.py") else
                                         "Recheck live candidates, skip impossible support and evaluate the final PnP hypothesis."
                                         if name.endswith("/relocalizer.py") else
                                         "Honor the native total and per-call RANSAC iteration budgets, including early success."
                                         if name.endswith("/MLPnPsolver.cpp") else
                                         "Import optional dense, GUI, and nonselected loop engines only on use.")})
        artifact = {"pyslam_commit": PYSLAM_COMMIT, "patches": manifest}
        (self.source / "biome-patches.json").write_text(json.dumps(artifact, sort_keys=True, indent=2))
        self.record("patches", artifact)

    def native(self):
        # Upstream CMake points here. Keep this alias within the dedicated vendor tree.
        gtsam = self.source / "thirdparty/gtsam_local/install"
        gtsam.parent.mkdir(parents=True, exist_ok=True)
        if not gtsam.exists():
            gtsam.symlink_to(self.prefix, target_is_directory=True)
        if gtsam.resolve() != self.prefix:
            raise RuntimeError("Existing GTSAM prefix alias points outside the isolated environment.")
        pybind = self.source / "thirdparty/gtsam_factors/pybind11"
        target = self.source / "cpp/pybind11"
        if not (target / "CMakeLists.txt").is_file():
            raise RuntimeError("Pinned vendored pybind11 is missing.")
        if pybind.is_symlink() and not pybind.exists():
            expected = subprocess.check_output(
                ["/usr/bin/git", "-C", str(self.source), "show",
                 PYSLAM_COMMIT + ":thirdparty/gtsam_factors/pybind11"], text=True).strip()
            if str(pybind.readlink()) != expected:
                raise RuntimeError("Unrecognized broken pybind11 alias; leaving it intact.")
            pybind.unlink()
        if not pybind.exists():
            pybind.symlink_to(target, target_is_directory=True)
        if pybind.resolve() != target.resolve():
            raise RuntimeError("GTSAM factors pybind11 alias differs from the pinned source.")
        headers = self.source / "thirdparty/gtsam_factors/include/gtsam_factors"
        headers.parent.mkdir(parents=True, exist_ok=True)
        header_source = self.source / "thirdparty/gtsam_factors"
        if not headers.exists():
            headers.symlink_to(header_source, target_is_directory=True)
        if headers.resolve() != header_source.resolve():
            raise RuntimeError("GTSAM factor headers alias differs from pinned source.")
        cmake = self.prefix / "bin/cmake"
        common = [
            "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release",
            "-DCMAKE_C_COMPILER=/usr/bin/clang", "-DCMAKE_CXX_COMPILER=/usr/bin/clang++",
            "-DCMAKE_OSX_ARCHITECTURES=arm64", "-DCMAKE_PREFIX_PATH=" + str(self.prefix),
            "-DCMAKE_IGNORE_PATH=/opt/homebrew",
            "-DPython3_EXECUTABLE=" + str(self.python), "-DPYTHON_EXECUTABLE=" + str(self.python),
            "-DWITH_PYTHON_INTERP_CHECK=ON",
            "-DOpenCV_DIR=" + str(self.prefix / "lib/cmake/opencv4"),
            "-DEigen3_DIR=" + str(self.prefix / "share/eigen3/cmake"),
            "-DCMAKE_BUILD_RPATH=" + str(self.prefix / "lib"),
            "-DCMAKE_MODULE_LINKER_FLAGS=-L" + str(self.prefix / "lib"),
            "-DCMAKE_SHARED_LINKER_FLAGS=-L" + str(self.prefix / "lib"),
        ]
        steps = [
            ("thirdparty/g2opy", [
                "-DBUILD_CSPARSE=OFF", "-DG2O_USE_OPENGL=OFF", "-DG2O_USE_OPENMP=OFF",
                "-DG2O_BUILD_APPS=OFF", "-DG2O_BUILD_EXAMPLES=OFF", "-DBUILD_WITH_MARCH_NATIVE=OFF",
                "-DCHOLMOD_INCLUDE_DIR=" + str(self.prefix / "include/suitesparse"),
                "-DCHOLMOD_LIBRARY=" + str(self.prefix / "lib/libcholmod.dylib"),
                "-DCSPARSE_INCLUDE_DIR=" + str(self.prefix / "include/suitesparse"),
                "-DCSPARSE_LIBRARY=" + str(self.prefix / "lib/libcxsparse.dylib")], False),
            ("thirdparty/orbslam2_features", [], False),
            ("thirdparty/pydbow3/modules/dbow3", [
                "-DCMAKE_INSTALL_PREFIX=" + str(self.source / "thirdparty/pydbow3/modules/dbow3/install"),
                "-DBUILD_EXAMPLES=OFF", "-DBUILD_TESTS=OFF"], True),
            ("thirdparty/pydbow3", ["-DBUILD_WITH_MARCH_NATIVE=OFF"], False),
            ("thirdparty/gtsam_factors", [], False),
            ("cpp", ["-DWITH_MARCH_NATIVE=OFF"], False),
        ]
        for relative, options, install in steps:
            source = self.source / relative
            build = source / "build"
            label = "native-" + relative.replace("/", "-")
            # Fresh CMake cache prevents an earlier missing-SuiteSparse fallback persisting.
            self.run([cmake, "--fresh", "-S", source, "-B", build] + common + options, label)
            self.run([cmake, "--build", build, "--parallel", str(self.jobs)], label)
            if install:
                self.run([cmake, "--install", build], label)
        binaries = [{"path": str(p.relative_to(self.source)), "sha256": sha256(p)}
                    for folder in ("cpp/lib", "thirdparty/g2opy/lib",
                                   "thirdparty/orbslam2_features/lib", "thirdparty/pydbow3/lib",
                                   "thirdparty/gtsam_factors/lib")
                    for p in sorted((self.source / folder).glob("*.so"))]
        self.record("native", {"qualified_profile": "python-core-orb2-g2o-dbow3",
                              "unqualified_optional_optimizers": ["gtsam"],
                              "common_flags": common, "binaries": binaries,
                              "prefix_aliases": {str(gtsam.relative_to(self.source)): str(self.prefix),
                                                str(pybind.relative_to(self.source)): str(target),
                                                str(headers.relative_to(self.source)): str(header_source)}})

    def python_packages(self):
        # Preserve the detector's torch and torchvision. NumPy and OpenCV must
        # remain ABI-compatible with the C++ extensions.
        query = ("import importlib.metadata as m,json; out={}; "
                 "\nfor n in ['torch','torchvision']:"
                 "\n try: out[n]=m.version(n)"
                 "\n except m.PackageNotFoundError: pass"
                 "\nprint(json.dumps(out))")
        installed = json.loads(subprocess.check_output([str(self.python), "-c", query], text=True))
        if not {"torch", "torchvision"}.issubset(installed):
            raise RuntimeError("Run setup_detector.py with this isolated environment before the SLAM Python phase.")
        constraints = self.state / "slam-constraints.txt"
        constraints.write_text("numpy==1.26.4\n" +
            "".join(name + "==" + version + "\n" for name, version in installed.items()) +
            "opencv-python==0\nopencv-python-headless==0\n"
            "opencv-contrib-python==0\nopencv-contrib-python-headless==0\n")
        self.run([self.python, "-m", "pip", "install", "--constraint", constraints, "-r",
                  Path(__file__).with_name("requirements-slam.txt")], "python-packages")
        self.run([self.python, "-m", "pip", "freeze"], "pip-freeze")
        self.record("python_packages", {"requirements_sha256":
                    sha256(Path(__file__).with_name("requirements-slam.txt")),
                    "protected_versions": installed, "freeze_log": str(self.logs / "pip-freeze.log")})

    def vocabulary(self):
        destination = self.source / "data/ORBvoc.txt"
        if not destination.exists():
            temporary = destination.with_suffix(".txt.download")
            script = "import gdown,sys; gdown.download(sys.argv[1],sys.argv[2],quiet=False)"
            self.run([self.python, "-c", script, VOCABULARY_URL, temporary], "orb-vocabulary")
            if not temporary.is_file() or temporary.stat().st_size < 1000000:
                raise RuntimeError("ORB vocabulary download did not produce a vocabulary.")
            temporary.replace(destination)
        with destination.open() as source:
            header = source.readline().strip().split()
        if len(header) != 4 or not all(token.isdigit() for token in header):
            raise RuntimeError("ORB vocabulary header is invalid.")
        self.record("vocabulary", {"url": VOCABULARY_URL, "path": str(destination),
                                  "sha256": sha256(destination), "bytes": destination.stat().st_size})

    def smoke(self):
        probe = (
            "import sys; sys.path.insert(0,sys.argv[1]); "
            "from pyslam.config import Config; Config(); "
            "from pyslam.config_parameters import Parameters; "
             "Parameters.USE_CPP_CORE=False; "
            "Parameters.kOptimizationFrontEndUseGtsam=False; "
            "Parameters.kOptimizationBundleAdjustUseGtsam=False; "
            "Parameters.kOptimizationLoopClosingUseGtsam=False; "
            "from pyslam.slam import Slam,PinholeCamera; "
            "import cv2,torch,gtsam,g2o,pydbow3,pyslam_utils,numpy; "
            "print({'numpy':numpy.__version__,'opencv':cv2.__version__,"
            "'torch':torch.__version__,'mps':torch.backends.mps.is_available(),"
            "'engine':'real pySLAM ORB/DBoW3 imports passed'})"
        )
        path = self.run([self.python, "-c", probe, self.source], "smoke")
        self.record("smoke", {"available": True, "profile": "python-core-orb2-g2o-dbow3",
                             "gtsam_optimizer_qualified": False, "log": str(path),
                             "qualification": "Import check only; run real sequence checkpoint/relocalization qualification."})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default=str(Path.home() / ".local/share/biome-robot-vision"))
    parser.add_argument("--conda", default=str(Path.home() / "miniconda3/bin/conda"))
    parser.add_argument("--jobs", type=int, default=4, help="Native compile concurrency, not a runtime limit")
    parser.add_argument("--phase", choices=("all", "environment", "source", "patches", "native",
                                          "python", "vocabulary", "smoke"), default="all")
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    installer = Installer(args.state_dir, args.conda, args.jobs)
    actions = {"environment": installer.environment, "source": installer.checkout,
               "patches": installer.patches, "native": installer.native,
               "python": installer.python_packages, "vocabulary": installer.vocabulary,
               "smoke": installer.smoke}
    phases = actions if args.phase == "all" else [args.phase]
    for phase in phases:
        actions[phase]()


if __name__ == "__main__":
    main()
