import importlib.util
import plistlib
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    'macos_controller_startup', Path(__file__).resolve().parents[1] /
    'deploy/repair-macos-controller-startup.py')
startup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(startup)


class MacControllerStartup(unittest.TestCase):
    def test_preview_apply_preserves_configuration_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            originals = {}
            for label in startup.LABELS:
                p = Path(root) / (label + '.plist')
                data = {'Label': label, 'ProgramArguments': ['/opt/python3', '-m', 'biomerte'],
                        'EnvironmentVariables': {'PYTHONPATH': '/pinned/release'},
                        'KeepAlive': True, 'RunAtLoad': True}
                p.write_bytes(plistlib.dumps(data)); p.chmod(0o600)
                originals[p] = (p.read_bytes(), data)
            self.assertEqual(len(startup.repair(root)), 2)
            for p, (raw, _) in originals.items():
                self.assertEqual(p.read_bytes(), raw)
            self.assertEqual(len(startup.repair(root, apply=True)), 2)
            for p, (raw, data) in originals.items():
                actual = plistlib.loads(p.read_bytes())
                self.assertEqual(actual.pop('ProgramArguments'), ['/opt/python3', '-S', '-m', 'biomerte'])
                expected = dict(data); expected.pop('ProgramArguments')
                self.assertEqual(actual, expected)
                self.assertEqual(p.stat().st_mode & 0o777, 0o600)
                self.assertEqual(next(p.parent.glob(p.name + '.before-no-site.*')).read_bytes(), raw)
            self.assertEqual(startup.repair(root, apply=True), [])
            self.assertEqual(len(list(Path(root).glob('*.before-no-site.*'))), 2)

    def test_invalid_second_agent_does_not_modify_first(self):
        with tempfile.TemporaryDirectory() as root:
            paths = [Path(root) / (label + '.plist') for label in startup.LABELS]
            for p, executable in zip(paths, ['/opt/python3', '/bin/bash']):
                p.write_bytes(plistlib.dumps({'ProgramArguments': [executable]}))
            original = paths[0].read_bytes()
            with self.assertRaises(ValueError):
                startup.repair(root, apply=True)
            self.assertEqual(paths[0].read_bytes(), original)
            self.assertEqual(list(Path(root).glob('*.before-no-site.*')), [])
