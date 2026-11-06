import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


class TerminalReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        provider = types.ModuleType('mac_provider'); provider.ROOT = self.root; provider.Remote = object
        terminal = types.ModuleType('terminal'); terminal.check_argv = lambda argv, profile: argv
        terminal.store_stream = lambda path, remote, size: (path.read_text(), {'size': path.stat().st_size})
        limits = types.ModuleType('terminal_limits'); limits.read_limits = lambda root: {}; limits.apply_limits = lambda limits: None
        adapter = Path(__file__).with_name('terminal_adapter.py')
        if not adapter.exists():
            adapter = Path(__file__).resolve().parents[1] / 'providers/mac-terminal/terminal_adapter.py'
        spec = importlib.util.spec_from_file_location('receipt_adapter', adapter)
        self.adapter = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'mac_provider': provider, 'terminal': terminal, 'terminal_limits': limits}):
            spec.loader.exec_module(self.adapter)
        self.cfg = {'terminal_profile': {'executable': '/bin/bash', 'executable_sha256': 'test'}}
        self.counter = self.root / 'counter'
        self.request = {'operation': 'one', 'inputs': {'argv': ['/bin/bash', '-c', 'echo run >> "' + str(self.counter) + '"; printf output']}}

    def test_failed_upload_retains_exact_output_without_reexecution(self):
        with patch.object(self.adapter, 'store_stream', side_effect=ConnectionError('offline')):
            with self.assertRaises(ConnectionError): self.adapter.execute(self.request, self.cfg, object())
        receipt = next((self.root / 'terminal-results').glob('*/receipt.json'))
        self.assertEqual(json.loads(receipt.read_text())['state'], 'finished')
        self.assertEqual((receipt.parent / 'stdout').read_text(), 'output')
        result = self.adapter.execute(self.request, self.cfg, object())
        self.assertEqual(result['stdout'], 'output'); self.assertTrue(result['evidence']['quiescent'])
        self.assertEqual(self.counter.read_text(), 'run\n')

    def test_changed_request_is_rejected_and_unfinished_launch_is_never_replayed(self):
        self.adapter.execute(self.request, self.cfg, object())
        changed = dict(self.request, inputs={'argv': ['/bin/bash', '-c', 'true']})
        with self.assertRaisesRegex(RuntimeError, 'identity conflict'): self.adapter.execute(changed, self.cfg, object())
        receipt = next((self.root / 'terminal-results').glob('*/receipt.json'))
        value = json.loads(receipt.read_text()); value['state'] = 'running'; receipt.write_text(json.dumps(value))
        with self.assertRaisesRegex(RuntimeError, 'unresolved'): self.adapter.execute(self.request, self.cfg, object())
        self.assertEqual(self.counter.read_text(), 'run\n')

    def test_worker_recovery_republishes_finished_receipt_and_preserves_identity(self):
        with patch.object(self.adapter, 'store_stream', side_effect=ConnectionError()):
            with self.assertRaises(ConnectionError): self.adapter.execute(self.request, self.cfg, object())
        db = sqlite3.connect(self.root / 'worker.sqlite'); db.execute('CREATE TABLE actions(id TEXT,state TEXT)'); db.execute("INSERT INTO actions VALUES('one','uncertain')"); db.commit(); db.close()
        calls = []
        worker = types.ModuleType('biomerte.worker')
        worker.reconcile_receipt = lambda directory, remote, operation, state, result: calls.append((operation, state, result))
        class Remote:
            def call(inner, name, args):
                self.assertEqual(name, 'cap.status'); self.assertEqual(args['operation'], 'one')
                return self.request
        with patch.dict(sys.modules, {'biomerte': types.ModuleType('biomerte'), 'biomerte.worker': worker}):
            self.adapter.recover_finished(Remote(), self.cfg)
        self.assertEqual(calls[0][0:2], ('one', 'succeeded')); self.assertEqual(calls[0][2]['stdout'], 'output')
        self.assertEqual(self.counter.read_text(), 'run\n')

    def test_no_unsolicited_duration_limit(self):
        result = self.adapter.execute({'operation': 'slow', 'inputs': {'argv': ['/bin/bash', '-c', 'sleep .05; echo done']}}, self.cfg, object())
        self.assertEqual(result['stdout'], 'done\n'); self.assertEqual(result['exit_code'], 0)

    def test_one_conflicting_receipt_does_not_block_independent_publication(self):
        requests = {op: dict(self.request, operation=op) for op in ('bad', 'good')}
        for request in requests.values(): self.adapter.execute(request, self.cfg, object())
        for receipt in (self.root / 'terminal-results').glob('*/receipt.json'):
            value = json.loads(receipt.read_text())
            if value['operation'] == 'bad':
                value['signature'] = 'mismatched'; receipt.write_text(json.dumps(value))
        db = sqlite3.connect(self.root / 'worker.sqlite'); db.execute('CREATE TABLE actions(id TEXT,state TEXT)')
        db.executemany("INSERT INTO actions VALUES(?,'uncertain')", [(op,) for op in requests]); db.commit(); db.close()
        calls = []; worker = types.ModuleType('biomerte.worker')
        worker.reconcile_receipt = lambda directory, remote, operation, state, result: calls.append(operation)
        class Remote:
            def call(inner, name, args): return requests[args['operation']]
        with patch.dict(sys.modules, {'biomerte': types.ModuleType('biomerte'), 'biomerte.worker': worker}):
            failures = self.adapter.recover_finished(Remote(), self.cfg)
        self.assertEqual(calls, ['good']); self.assertEqual(failures, [{'operation': 'bad', 'error': 'RuntimeError'}])
        self.assertEqual(self.counter.read_text(), 'run\nrun\n')


if __name__ == '__main__': unittest.main()
