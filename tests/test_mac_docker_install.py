"""Exercise Mac slot sizing, persistence and launchd replacement without a live daemon."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


class MacDockerInstall(unittest.TestCase):
    def test_slot_selection_and_delayed_replacement(self):
        path = Path(__file__).resolve().parents[1] / 'providers/mac-docker/mac_docker.py'
        spec = importlib.util.spec_from_file_location('mac_docker_install_test', str(path))
        provider = importlib.util.module_from_spec(spec)
        gateway = SimpleNamespace(save_json=lambda p, v: p.write_text(json.dumps(v)))
        with mock.patch.dict(sys.modules, {'mac_provider': gateway}), mock.patch.object(sys, 'path', sys.path[:]):
            spec.loader.exec_module(provider)
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            provider.HOME, provider.STATE, provider.TERMINAL = home, home / 'state', home / 'terminal'
            (provider.TERMINAL / 'identity').mkdir(parents=True)
            (provider.TERMINAL / 'identity/node.json').write_text('{"node":"test-node"}')
            (provider.TERMINAL / 'terminal-manifest.json').write_text('{"site":"test-site"}')
            launches = []
            loaded = False

            def launchctl(argv, **kwargs):
                nonlocal loaded
                status = 0
                if argv[1] == 'print' and argv[2].endswith(provider.LABEL):
                    status = 0 if loaded else 1
                if argv[1] == 'bootstrap':
                    launches.append(argv)
                    status = 5 if len(launches) == 2 else 0
                    loaded = status == 0
                return subprocess.CompletedProcess(argv, status, b'', b'')

            with mock.patch.object(sys, 'platform', 'darwin'), mock.patch.object(provider.os, 'cpu_count', return_value=8), \
                    mock.patch.object(provider.subprocess, 'run', side_effect=launchctl), \
                    mock.patch.object(provider.time, 'sleep'), mock.patch.object(provider.os, 'umask'):
                for flags, expected in (([], 32), (['--slots','17'], 17), ([], 17)):
                    with mock.patch.object(sys, 'argv', ['mac_docker.py','install','--executable','/bin/sh'] + flags):
                        provider.install()
                    self.assertEqual(json.loads((provider.STATE / 'manifest.json').read_text())['slots'], expected)
                self.assertEqual(len(launches), 4)
