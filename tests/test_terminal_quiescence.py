"""Recovery proofs for a terminal adapter which exited before saving its result."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock
from biomerte.store import digest
from tests.test_terminal_stream_retry import load_source, ROOT


class TerminalQuiescence(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        mac = types.SimpleNamespace(ROOT=root, Remote=object)
        limits = types.SimpleNamespace(read_limits=None, apply_limits=None)
        terminal = types.SimpleNamespace(store_stream=lambda *a: ('', {}), check_argv=None)
        with mock.patch.dict(sys.modules, mac_provider=mac, terminal=terminal, terminal_limits=limits):
            self.adapter = load_source(ROOT / 'providers/mac-terminal/terminal_adapter.py', 'quiescence_adapter')
        self.op = 'interrupted-upload'
        self.action = dict(id=self.op, contract='host.terminal.run:1', inputs={'argv': ['tool']},
                           resource='terminal.mac', generation=2, lease='lease', state='uncertain',
                           provider='mac', custody={'state': 'finished'})
        self.remote = mock.Mock(node='mac')
        self.remote.call.return_value = self.action
        self.settle = mock.Mock()
        key = hashlib.sha256(self.op.encode()).hexdigest()
        self.adapter_dir = root / 'terminal-results' / key
        self.runner_dir = root / 'command-runners' / key
        self.adapter_dir.mkdir(parents=True)
        self.runner_dir.mkdir(parents=True)
        signature = digest([self.action[k] for k in ('contract', 'inputs', 'resource', 'generation', 'lease')])
        self.write(self.adapter_dir, 'receipt', dict(operation=self.op, signature=digest([self.op, self.action['inputs'], {}]),
                   adapter_pid=51, state='running', pid=52, pgid=52, retention_version=1))
        self.write(self.runner_dir, 'state', dict(operation=self.op, signature=signature, adapter_pid=51))
        self.write(self.runner_dir, 'completion', dict(state='finished', returncode=1))
        self.kill = mock.patch.object(os, 'killpg', side_effect=ProcessLookupError())
        self.check_group = self.kill.start()
        self.addCleanup(self.kill.stop)

    def write(self, directory, name, value):
        (directory / (name + '.json')).write_text(json.dumps(value))

    def recover(self):
        self.adapter.recover_one_finished(self.remote, {'terminal_profile': {}}, self.op, self.settle)

    def test_exact_failed_runner_and_absent_process_group_settle_without_replay(self):
        self.recover()
        self.settle.assert_called_once()
        args = self.settle.call_args[0]
        self.assertEqual(args[2:4], (self.op, 'failed'))
        self.assertTrue(args[4]['evidence']['quiescent'])
        self.assertTrue(args[4]['effects_unknown'])
        self.assertFalse(args[4]['replay_allowed'])
        self.check_group.assert_called_once_with(52, 0)

    def test_live_or_unobservable_group_remains_in_custody(self):
        for error in (None, PermissionError()):
            self.check_group.side_effect = error
            self.recover()
        self.settle.assert_not_called()

    def test_old_generation_or_unfinished_runner_cannot_settle(self):
        self.action['generation'] += 1
        self.recover()
        self.action['generation'] -= 1
        (self.runner_dir / 'completion.json').unlink()
        self.recover()
        self.settle.assert_not_called()

    def test_finished_output_is_durable_before_controller_settlement(self):
        receipt_path = self.adapter_dir / 'receipt.json'
        receipt = json.loads(receipt_path.read_text())
        receipt.update(state='finished', result={'exit_code': 0, 'evidence': {'quiescent': True}})
        self.write(self.adapter_dir, 'receipt', receipt)
        published = self.adapter_dir / 'published.json'

        def reply_lost(directory, remote, operation, state, result):
            self.assertEqual(json.loads(published.read_text()), result)
            raise ConnectionError('controller committed but reply was lost')

        self.settle.side_effect = reply_lost
        with self.assertRaises(ConnectionError):
            self.recover()
        result = json.loads(published.read_text())
        self.assertEqual(self.adapter.acknowledge_result({'operation': self.op,
                                                         'result_digest': digest(result)}),
                         {'acknowledged': True})
        self.assertFalse(published.exists())

    def test_proven_failed_command_can_retry_settlement_and_retire(self):
        output = self.adapter_dir / 'stdout'
        output.write_text('retained output')
        self.settle.side_effect = ConnectionError('reply lost')
        with self.assertRaises(ConnectionError):
            self.recover()
        first_outcome = self.settle.call_args[0][4]
        # The PID can be reused before an exact, separately retried settlement.
        self.check_group.side_effect = None
        self.settle.side_effect = None
        self.settle.reset_mock()
        self.recover()
        self.settle.assert_called_once()
        self.assertEqual(self.settle.call_args[0][3:], ('failed', first_outcome))
        self.check_group.assert_called_once_with(52, 0)
        with self.assertRaisesRegex(RuntimeError, 'result mismatch'):
            self.adapter.acknowledge_result({'operation': self.op, 'result_digest': 'different'})
        self.assertTrue(output.exists())
        request = {'operation': self.op, 'result_digest': digest(first_outcome)}
        for _ in range(2):
            self.assertEqual(self.adapter.acknowledge_result(request), {'acknowledged': True})
        self.assertFalse(output.exists())

    def test_existing_failed_settlement_can_retire_with_legacy_running_receipt(self):
        # The old recovery path left a running receipt and a published failure.
        self.recover()
        outcome = self.settle.call_args[0][4]
        receipt = json.loads((self.adapter_dir / 'receipt.json').read_text())
        receipt['state'] = 'running'
        receipt.pop('result', None)
        self.write(self.adapter_dir, 'receipt', receipt)
        self.write(self.adapter_dir, 'published', outcome)
        request = {'operation': self.op, 'result_digest': digest(outcome)}
        self.assertEqual(self.adapter.acknowledge_result(request), {'acknowledged': True})

    def test_running_receipt_requires_published_quiescence_proof(self):
        outcome = {'reason': 'terminal_adapter_exited_before_recording_outcome',
                   'evidence': {'quiescent': True, 'process_group_absent': True,
                                'adapter_pid': 51, 'pgid': 90}}
        self.write(self.adapter_dir, 'published', outcome)
        request = {'operation': self.op, 'result_digest': digest(outcome)}
        with self.assertRaisesRegex(RuntimeError, 'Unresolved terminal output'):
            self.adapter.acknowledge_result(request)
        self.assertTrue((self.adapter_dir / 'published.json').exists())
