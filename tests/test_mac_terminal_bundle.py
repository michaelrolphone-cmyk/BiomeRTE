"""Keep the shipped macOS payload on the same worker and admission rules as the Pi."""
import importlib.util
import io
import json
import plistlib
from pathlib import Path
import runpy
import sys
import subprocess
import textwrap
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]


class MacTerminalBundle(unittest.TestCase):
    def test_clean_installer_starts_real_worker_transport_and_recovery(self):
        build = runpy.run_path(str(ROOT / 'providers/mac-terminal/build_installer.py'))['bundle']
        with tempfile.TemporaryDirectory() as directory, zipfile.ZipFile(io.BytesIO(build())) as archive:
            archive.extractall(directory)
            probe = textwrap.dedent("""
                import sys
                from pathlib import Path
                root = Path.cwd()
                sys.path[:0] = [str(root), str(root / 'runtime')]
                from biomerte.worker import run_worker
                class Remote:
                    node = 'test-provider'
                    site = 'test-site'
                    realm = 'site'
                    def call(self, name, args, request_id=None):
                        assert name == 'provider.poll'
                        return {'fences': [], 'actions': []}
                assert run_worker('.', Remote(), {'offers': []}, once=True) == 0
                import terminal_receipt_recovery
                assert terminal_receipt_recovery.recover_finished(Remote(), {}) == []
                import persistent_transport, websocket
                assert root in Path(websocket.__file__).resolve().parents
            """)
            subprocess.run([sys.executable, '-I', '-S', '-c', probe], cwd=directory, check=True)

    def test_launchd_jobs_use_packaged_entry_points_and_enrolled_account(self):
        source = (ROOT / 'providers/mac-terminal/installer.sh.in').read_text()
        builder = source.split("<<'PLIST_PY'\n", 1)[1].split('\nPLIST_PY', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            labels = ('org.biomerte.mac-terminal', 'org.biomerte.terminal-receipt-recovery')
            subprocess.run([sys.executable, '-', directory, sys.executable, 'enrolled', directory, *labels],
                           input=builder, text=True, check=True)
            for filename, label, script in zip(('service.plist', 'receipt-recovery.plist'), labels,
                                              ('mac_provider.py', 'terminal_receipt_recovery.py')):
                job = plistlib.loads((Path(directory) / filename).read_bytes())
                self.assertEqual((job['Label'], job['UserName']), (label, 'enrolled'))
                self.assertIn(str(Path(directory) / script), job['ProgramArguments'])
                self.assertTrue(job['KeepAlive'] and job['RunAtLoad'])

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
