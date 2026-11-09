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
    return patch_headless_image_cache(source)



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


PATCHES = {
    "pyslam/slam/optimizer_gtsam.py": patch_gtsam_factor_imports,
    "thirdparty/g2opy/python/CMakeLists.txt": patch_g2o_link,
    "pyslam/slam/slam.py": patch_slam,
    "pyslam/loop_closing/loop_detector_configs.py": patch_loop_configs,
    "pyslam/loop_closing/loop_closing.py": patch_loop_viewer,
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
                             "reason": ("Link the optional g2o OpenGL helper only when its target exists."
                                        if name.endswith("CMakeLists.txt") else
                                        "Import optional GTSAM native factors only inside functions that use them."
                                        if name.endswith("optimizer_gtsam.py") else
                                        "Keep optional dense/GUI imports lazy and preserve headless mode on restore."
                                        if name.endswith("/slam.py") else
                                        "Keep headless loop visualization images out of memory and checkpoints; import GUI lazily."
                                        if name.endswith("/loop_closing.py") else
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
