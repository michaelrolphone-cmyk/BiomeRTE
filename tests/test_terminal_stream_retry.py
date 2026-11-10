"""Terminal output delivery must never repeat a settled native command."""
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_source(path, name):
    module = types.ModuleType(name)
    module.__file__ = str(path)
    exec(compile(path.read_text(), str(path), 'exec'), module.__dict__)
    return module


class Transport:
    def __init__(self, failures=2):
        self.failures = failures
        self.calls = []
    def call(self, name, args, rid=None):
        self.calls.append((name, dict(args), rid))
        if self.failures:
            self.failures -= 1
            raise RuntimeError('D1_ERROR: internal error after possible durable upload')
        block = base64.b64decode(args['data'])
        return {'sha256': hashlib.sha256(block).hexdigest(), 'size': len(block)}


class StreamRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'stdout'
        self.path.write_bytes(b'exact retained output\n')
        self.terminal = load_source(ROOT / 'providers/pi-terminal/terminal.py', 'retry_terminal')
        self.delays = []
        self.terminal.time = types.SimpleNamespace(sleep=self.delays.append)

    def test_lost_upload_ack_reuses_exact_request_and_retained_bytes(self):
        remote = Transport()
        preview, stream = self.terminal.store_stream(str(self.path), remote, 8)
        self.assertEqual(remote.calls[0], remote.calls[1])
        self.assertEqual(remote.calls[1], remote.calls[2])
        self.assertEqual(self.delays, [5, 5])
        self.assertEqual(self.path.read_bytes(), b'exact retained output\n')
        self.assertEqual(stream['size'], self.path.stat().st_size)
        self.assertEqual(stream['sha256'], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(preview, 'exact re')
        self.assertNotEqual(remote.calls[2][2], remote.calls[3][2])

    def test_receipt_corruption_stays_visible(self):
        remote = Transport(0)
        remote.call = lambda *args: {'sha256': 'wrong', 'size': 1}
        with self.assertRaises(IOError):
            self.terminal.store_stream(str(self.path), remote)
        self.assertEqual(self.delays, [])

    def test_interrupt_preserves_original_output(self):
        remote = Transport(0)
        def interrupted(*args):
            raise KeyboardInterrupt()
        remote.call = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.terminal.store_stream(str(self.path), remote)
        self.assertEqual(self.path.read_bytes(), b'exact retained output\n')

    @unittest.skipUnless(sys.platform == 'darwin', 'native Mac adapter qualification')
    def test_native_command_runs_once_across_failed_uploads(self):
        from biomerte import command_watch
        home = Path(self.temp.name)
        marker = home / 'executions'
        mac = types.ModuleType('mac_provider')
        mac.ROOT, mac.Remote = home, object
        limits = types.ModuleType('terminal_limits')
        limits.read_limits = lambda root: {}
        limits.apply_limits = lambda value: None
        with mock.patch.dict(sys.modules, {'mac_provider': mac, 'terminal': self.terminal,
                                           'terminal_limits': limits}):
            adapter = load_source(ROOT / 'providers/mac-terminal/terminal_adapter.py', 'retry_adapter')
            executable = os.path.realpath('/bin/bash')
            config = {'terminal_profile': {'executable': executable,
                       'executable_sha256': hashlib.sha256(Path(executable).read_bytes()).hexdigest()}}
            request = {'operation': 'one-effect', 'inputs': {'argv':
                       [executable, '-c', "echo once >> '" + str(marker) + "'; echo result"]}}
            result = adapter.execute(request, config, Transport())
            self.assertEqual(result['exit_code'], 0)
            self.assertEqual(result['stdout'], 'result\n')
            self.assertEqual(marker.read_text(), 'once\n')
            # Reattaching to the exact saved receipt republishes only output.
            again = adapter.execute(request, config, Transport(0))
            self.assertEqual(again['stdout'], 'result\n')
            self.assertEqual(marker.read_text(), 'once\n')


if __name__ == '__main__':
    unittest.main()
