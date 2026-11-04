"""Keep the shipped macOS payload on the same worker and admission rules as the Pi."""
import importlib.util
import io
import json
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]


class MacTerminalBundle(unittest.TestCase):
    def test_installer_uses_shared_worker_profile_and_sixteen_slots(self):
        build = runpy.run_path(str(ROOT / 'providers/mac-terminal/build_installer.py'))['bundle']
        with tempfile.TemporaryDirectory() as directory, zipfile.ZipFile(io.BytesIO(build())) as z:
            z.extractall(directory)
            target = Path(directory)
            self.assertEqual((target / 'runtime/biomerte/worker.py').read_bytes(),
                             (ROOT / 'biomerte/worker.py').read_bytes())
            self.assertEqual((target / 'terminal.py').read_bytes(),
                             (ROOT / 'providers/pi-terminal/terminal.py').read_bytes())
            spec = importlib.util.spec_from_file_location('mac_pool_test', str(target / 'mac_provider.py'))
            provider = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(provider)
            (target / 'identity').mkdir()
            (target / 'identity/node.json').write_text(json.dumps({'node': 'existing-node'}))
            (target / 'terminal-manifest.json').write_text(json.dumps({
                'site': 'site-1', 'offers': [{'resource': 'terminal.mac.keep-this-id'}]}))
            with mock.patch.object(provider, 'invoke', return_value={'site':'site-1'}), \
                    mock.patch.object(sys, 'path', [directory] + sys.path):
                provider.configure()
                manifest = json.loads((target / 'terminal-manifest.json').read_text())
                self.assertEqual(manifest['parallelism'], 16)
                offer, = manifest['offers']
                self.assertEqual((offer['resource'], offer['capacity'], offer['contract']),
                                 ('terminal.mac.keep-this-id', 16, 'host.terminal.run:1'))
                with mock.patch('biomerte.worker.run_worker') as worker:
                    provider.worker()
                    self.assertEqual(worker.call_args[0][2], manifest)
                self.assertIn('artifact.get', offer['usage']['read_result'])
