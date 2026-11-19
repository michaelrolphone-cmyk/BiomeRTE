"""Run portable closure tests in isolation from the native controller package."""
import os
from pathlib import Path
import subprocess
import sys
import unittest

class PortableClosure(unittest.TestCase):
    def test_portable_transport_and_recovery_boundaries(self):
        root = Path(__file__).resolve().parents[1]
        test = root / "deploy/portable-services/tests/test_portable.py"
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        result = subprocess.run([sys.executable, str(test)], cwd=str(root), env=environment,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

if __name__ == "__main__":
    unittest.main()
