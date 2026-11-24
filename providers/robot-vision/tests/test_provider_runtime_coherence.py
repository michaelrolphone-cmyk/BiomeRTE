"""Fresh-process coverage for a persisted manifest selecting an older core."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

PROVIDER = Path(__file__).resolve().parents[1]
REPO = PROVIDER.parents[1]

# Stop before any remote registration. serve() has already selected and imported
# its worker core at this boundary; no production service or credentials are used.
STARTUP_PROBE = r"""
import importlib.util
import json
from pathlib import Path
import struct
import sys

source, state, expected_repo = map(Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location("runtime_probe_adapter", source)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)

def inspect_core(cfg):
    import biomerte.worker as worker
    import biomerte.stream_router as router
    # These are native live-feed bodies, including metadata larger than the
    # generic router limit. Both lane-specific decoder contracts must survive.
    for lane, metadata, image in (
        ("vision", {"type": "frame", "frame_id": "front:123",
                    "processed_monotonic": 123.0,
                    "sensors": {"imu": {"samples": [1, 2, 3]}},
                    "telemetry": "v" * 17000}, b"\xff\xd8jpeg\xff\xd9"),
        ("map", {"type": "map_snapshot", "map_id": "preserved-map",
                 "revision": 42, "landmarks": ["point"] * 3000}, b""),
    ):
        header = json.dumps(metadata).encode()
        body = struct.pack("!I", len(header)) + header + image
        decoded, payload = router.decode(body, 16 * 1024 * 1024, lane=lane)
        assert decoded == metadata and payload == image
        try:
            router.decode(body, 16 * 1024 * 1024, lane="other")
        except Exception as error:
            assert type(error).__name__ == "Fault", type(error).__name__
        else:
            raise AssertionError("generic metadata limit was relaxed")
    assert Path(worker.__file__).resolve() == expected_repo / "biomerte/worker.py"
    assert Path(router.__file__).resolve() == expected_repo / "biomerte/stream_router.py"
    offers = adapter.worker_offers(cfg)
    print(json.dumps({"cfg": cfg, "argv": offers[0]["argv"],
                      "worker": worker.__file__, "decoder": router.__file__}))
    raise SystemExit(0)

adapter.Remote = inspect_core
adapter.main(["serve", "--state", str(state)])
"""


class ProviderRuntimeTests(unittest.TestCase):
    def config(self, root, repo, **overrides):
        state = root / "state"
        state.mkdir()
        cfg = {
            "repo": str(repo), "state": str(state), "python": sys.executable,
            "provider": "provider-fixture", "resource": "robotvision.fixture",
            "site": "site-fixture", "slots": 2, "socket": str(state / "vision.sock"),
            "health_timeout_seconds": 5, "worker_timeout_seconds": None,
        }
        cfg.update(overrides)
        manifest = state / "provider-manifest.json"
        manifest.write_text(json.dumps(cfg, indent=2) + "\n")
        return cfg, manifest

    def run_startup(self, source, cfg, expected_repo):
        env = dict(os.environ)
        # Match a launch that names the new provider. In particular, do not
        # preload biomerte in this fresh process before serve() reads the manifest.
        env["PYTHONPATH"] = os.pathsep.join((str(source.parent), str(expected_repo)))
        return subprocess.run(
            [sys.executable, "-c", STARTUP_PROBE, str(source), cfg["state"], str(expected_repo)],
            cwd=cfg["state"], env=env, text=True, capture_output=True, timeout=15,
        )

    def test_bundled_startup_ignores_stale_manifest_core_for_both_lanes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old-release"
            package = old / "biomerte"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("")
            (package / "worker.py").write_text("class ActionPool: pass\nclass Worker: pass\n")
            (package / "stream_router.py").write_text(
                "def decode(message, maximum=None):\n    raise AssertionError('stale core')\n")
            cfg, manifest = self.config(root, old)
            original = manifest.read_bytes()
            result = self.run_startup(PROVIDER / "biome_provider.py", cfg, REPO)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["cfg"], dict(cfg, repo=str(REPO)))
            self.assertEqual(report["argv"][:4],
                             [cfg["python"], str(PROVIDER / "biome_provider.py"),
                              "execute", "--state"])
            self.assertEqual(manifest.read_bytes(), original)

    def test_standalone_adapter_retains_configured_core_and_explicit_adapter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            standalone = root / "installed-adapter"
            standalone.mkdir()
            source = standalone / "biome_provider.py"
            shutil.copy2(PROVIDER / "biome_provider.py", source)
            for contract in PROVIDER.glob("*.json"):
                shutil.copy2(contract, standalone / contract.name)
            cfg, manifest = self.config(root, REPO, adapter_path=str(source))
            original = manifest.read_bytes()
            result = self.run_startup(source, cfg, REPO)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["cfg"], cfg)
            self.assertEqual(report["argv"][1], str(source))
            self.assertEqual(manifest.read_bytes(), original)

    def test_runtime_selection_preserves_caller_and_explicit_adapter(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "runtime_config_adapter", PROVIDER / "biome_provider.py")
        adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adapter)
        cfg = {"repo": "/older/release", "provider": "provider-fixture",
               "resource": "robotvision.fixture", "state": "/persistent/state",
               "socket": "/persistent/state/vision.sock",
               "adapter_path": "/explicit/adapter.py", "lease": {"generation": 4}}
        original = json.loads(json.dumps(cfg))
        selected = adapter.runtime_config(cfg)
        self.assertEqual(cfg, original)
        self.assertIsNot(selected, cfg)
        self.assertEqual(selected, dict(original, repo=str(REPO)))


if __name__ == "__main__":
    unittest.main()
