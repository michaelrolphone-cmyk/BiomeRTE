"""Native deployment boundaries remain separate from the Python 3.11 authority."""
import ast
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class NativeCompatibilityTests(unittest.TestCase):
    def test_native_entry_sources_accept_python34_grammar(self):
        try:
            ast.parse("", feature_version=(3, 4))
        except (TypeError, ValueError):
            self.skipTest("Host parser cannot select Python 3.4; native smoke is still required")
        paths = [
            "biomerte/app.py", "biomerte/api.py", "biomerte/store.py",
            "biomerte/assistant_support.py", "biomerte/providers.py",
            "biomerte/streams.py", "biomerte/exchange.py", "biomerte/web.py",
            "biomerte/mcp_events.py", "biomerte/grok_receivers.py",
            "deploy/gateway-agent.py", "deploy/local_link.py",
            "deploy/mcp_events.py", "deploy/grok_receivers.py",
            "gateway/deploy/gateway-agent.py", "gateway/deploy/local_link.py",
            "gateway/deploy/mcp_events.py", "gateway/deploy/grok_receivers.py"]
        for path in paths:
            with self.subTest(path=path):
                ast.parse((ROOT / path).read_text(), filename=path, feature_version=(3, 4))

    def run_isolated(self, code, directory, python_path=""):
        env = dict(os.environ, PYTHONPATH=python_path, PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, "-c", code], cwd=str(directory),
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_app_import_never_loads_managed_daemon_modules(self):
        code = """
import sys
blocked = set(("biomerte.controller_authority", "biomerte.controller_events",
               "biomerte.controller_checkpoint", "biomerte.checkpoint_chunks",
               "biomerte.service_daemon"))
class BlockManaged:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked:
            raise AssertionError("Managed daemon eagerly imported: " + fullname)
sys.meta_path.insert(0, BlockManaged())
from biomerte.app import Application
import biomerte.web
assert not blocked.intersection(sys.modules)
"""
        with tempfile.TemporaryDirectory() as directory:
            self.run_isolated(code, directory, str(ROOT))

    def test_event_deployment_modules_work_without_an_installed_core_package(self):
        code = """
import sys
class BlockCore:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "biomerte" or fullname.startswith("biomerte."):
            raise AssertionError("Standalone event bundle imported core: " + fullname)
sys.meta_path.insert(0, BlockCore())
import mcp_events
import grok_receivers
store = mcp_events.EventStore(":memory:", lambda *args: {},
                             lambda *args: True, controller="source")
store.close()
assert "biomerte" not in sys.modules
"""
        for deploy in ("deploy", "gateway/deploy"):
            with self.subTest(deploy=deploy), tempfile.TemporaryDirectory() as directory:
                for name in ("mcp_events.py", "grok_receivers.py"):
                    shutil.copyfile(ROOT / deploy / name, Path(directory) / name)
                self.run_isolated(code, directory)


if __name__ == "__main__":
    unittest.main()
