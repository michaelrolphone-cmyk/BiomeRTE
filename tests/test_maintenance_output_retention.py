import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('maintenance_retention', str(ROOT / 'deploy/maintenance-agent/output_retention.py'))
r = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(r)
ZERO = {'output_count': 0, 'output_bytes': 0, 'result_count': 0, 'result_bytes': 0, 'max_age_seconds': 0}


class SimulatedCrash(BaseException):
    pass


class MaintenanceOutputRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(os.path.realpath(self.temp.name))
        self.db = sqlite3.connect(str(self.home / 'ledger.sqlite'))
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE commands(id TEXT PRIMARY KEY,state TEXT,result TEXT)')
        r.initialize(self.db)

    def job(self, identifier):
        return {'id': identifier, 'argv': ['/bin/echo', identifier], 'cwd': None}

    def folder(self, identifier):
        return self.home / 'commands' / hashlib.sha256(identifier.encode()).hexdigest()

    def make_output(self, identifier, text='recent output'):
        folder = self.folder(identifier)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'stdout').write_text(text)
        (folder / 'stderr').write_text('')
        (folder / 'pid').write_text('123')
        return {'state': 'succeeded', 'exit_code': 0, 'stdout': text, 'stderr': '',
                'output_directory': str(folder)}

    def finish(self, identifier, when=1, ack=True, text='recent output', quiescent=True):
        self.assertTrue(r.admit(self.db, self.job(identifier), now=when - 1))
        result = self.make_output(identifier, text)
        r.complete(self.db, identifier, result, quiescent=quiescent, now=when, home=self.home)
        if ack:
            self.assertTrue(r.acknowledge(self.db, identifier, result, self.receipt(identifier, result), 'mac', now=when + 1))
        return result

    def receipt(self, identifier, result):
        return {'accepted': True, 'id': identifier, 'host': 'mac', 'result_sha256': r.sha(result)}

    def trim(self, limits=None, now=100, **kwargs):
        return r.trim(self.db, self.home, ZERO if limits is None else limits, now=now,
                      active=kwargs.pop('active', lambda p: False), **kwargs)

    def row(self, identifier):
        state, result = self.db.execute('SELECT state,result FROM commands WHERE id=?', (identifier,)).fetchone()
        return state, json.loads(result) if result else None

    def test_exact_ack_required_and_stale_wrong_generic_acks_never_retire(self):
        result = self.finish('one', ack=False)
        good = self.receipt('one', result)
        bad = [{'accepted': True}, dict(good, accepted=False), dict(good, id='other'),
               dict(good, host='other'), dict(good, result_sha256='0' * 64)]
        for receipt in bad:
            self.assertFalse(r.acknowledge(self.db, 'one', result, receipt, 'mac'))
            self.assertEqual(self.trim()['outputs_retired'], [])
            self.assertEqual(self.row('one'), ('succeeded', result))
        self.assertTrue(r.acknowledge(self.db, 'one', result, good, 'mac'))
        self.assertEqual(self.trim()['outputs_retired'], ['one'])
        self.assertTrue(self.row('one')[1]['result_retired'])

    def test_delayed_ack_pins_expired_and_oversized_diagnostics_until_received(self):
        result = self.finish('one', when=1, ack=False, text='x' * 100000)
        self.assertEqual(self.trim(now=100000)['results_retired'], [])
        self.assertEqual((self.folder('one') / 'stdout').stat().st_size, 100000)
        r.acknowledge(self.db, 'one', result, self.receipt('one', result), 'mac', now=100000)
        report = self.trim(now=100001)
        self.assertEqual(report['results_retired'], ['one'])
        self.assertFalse((self.folder('one') / 'stdout').exists())

    def test_retry_lost_ack_and_ack_replay_are_idempotent(self):
        result = self.finish('one', ack=False)
        receipt = self.receipt('one', result)  # Remote committed, response was lost.
        self.assertFalse(r.admit(self.db, self.job('one')))
        self.assertEqual(self.trim()['results_retired'], [])
        self.assertTrue(r.acknowledge(self.db, 'one', result, receipt, 'mac', now=10))
        self.assertTrue(r.acknowledge(self.db, 'one', result, receipt, 'mac', now=20))
        self.assertEqual(self.db.execute('SELECT acked FROM maintenance_output_retention').fetchone()[0], 10)
        self.trim()
        self.assertTrue(r.acknowledge(self.db, 'one', result, receipt, 'mac', now=30))
        self.assertFalse(r.admit(self.db, self.job('one')))

    def test_legacy_rows_and_preexisting_outputs_never_become_eligible(self):
        for state in ['reported', 'succeeded', 'failed', 'uncertain']:
            result = self.make_output(state)
            self.db.execute('INSERT INTO commands VALUES(?,?,?)', (state, state, json.dumps(result)))
        self.db.commit()
        before = self.db.execute('SELECT * FROM commands ORDER BY id').fetchall()
        self.assertFalse(r.admit(self.db, self.job('reported')))
        self.trim()
        self.assertEqual(self.db.execute('SELECT * FROM commands ORDER BY id').fetchall(), before)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM maintenance_output_retention').fetchone()[0], 0)
        result = self.row('succeeded')[1]
        self.assertTrue(r.acknowledge(self.db, 'succeeded', result, {'accepted': True}, 'mac'))
        self.trim()
        self.assertEqual(self.row('succeeded'), ('reported', result))
        self.assertTrue(all((self.folder(s) / 'stdout').exists() for s in ['reported', 'succeeded', 'failed', 'uncertain']))

    def test_running_uncertain_unacknowledged_and_nonquiescent_commands_are_pinned(self):
        r.admit(self.db, self.job('running'));self.make_output('running')
        pending = self.finish('unacked', ack=False)
        self.finish('descendants', quiescent=False)
        r.admit(self.db, self.job('uncertain'));self.make_output('uncertain')
        result = {'state': 'uncertain', 'reason': 'Inspect running descendants'}
        r.complete(self.db, 'uncertain', result, quiescent=False, home=self.home)
        self.assertTrue(r.acknowledge(self.db, 'uncertain', result, self.receipt('uncertain', result), 'mac'))
        before = self.db.execute('SELECT * FROM commands ORDER BY id').fetchall()
        self.trim()
        self.assertEqual(before, self.db.execute('SELECT * FROM commands ORDER BY id').fetchall())
        self.assertTrue(all((self.folder(i) / 'stdout').exists() for i in ['running', 'unacked', 'descendants', 'uncertain']))

    def test_restart_changes_new_running_to_uncertain_without_eligibility_or_replay(self):
        r.admit(self.db, self.job('one'));self.make_output('one')
        r.recover_running(self.db)
        state, result = self.row('one')
        self.assertEqual(state, 'uncertain')
        self.assertIsNone(self.db.execute('SELECT completed FROM maintenance_output_retention').fetchone()[0])
        self.assertTrue(r.acknowledge(self.db, 'one', result, self.receipt('one', result), 'mac'))
        self.trim()
        self.assertTrue((self.folder('one') / 'stdout').exists())
        self.assertFalse(r.admit(self.db, self.job('one')))
        self.assertFalse(self.row('one')[1].get('result_retired', False))

    def test_command_admission_and_future_marker_commit_atomically(self):
        self.db.execute("CREATE TRIGGER fail_enroll BEFORE INSERT ON maintenance_output_retention BEGIN SELECT RAISE(ABORT,'crash'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            r.admit(self.db, self.job('one'))
        self.assertIsNone(self.db.execute('SELECT id FROM commands').fetchone())

    def test_replay_identity_fence_survives_payload_retirement(self):
        self.finish('one');self.trim()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM commands').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM maintenance_output_retention').fetchone()[0], 1)
        self.assertFalse(r.admit(self.db, self.job('one')))
        altered = dict(self.job('one'), argv=['/bin/echo', 'altered'])
        with self.assertRaisesRegex(ValueError, 'identity conflict'):
            r.admit(self.db, altered)

    def test_count_byte_and_age_limits_select_newest_completed_diagnostics(self):
        for i in range(6):
            self.finish(str(i), when=i + 1, text='x' * 8)
        limits = {'output_count': 2, 'output_bytes': 16, 'result_count': 2,
                  'result_bytes': 10000, 'max_age_seconds': 1000}
        report = self.trim(limits, now=7)
        self.assertEqual(set(report['outputs_retired']), {'0', '1', '2', '3'})
        self.assertEqual(set(report['results_retired']), {'0', '1', '2', '3'})
        self.assertEqual(report['retained_output_bytes'], 16)
        report = self.trim(dict(limits, max_age_seconds=1), now=100)
        self.assertEqual(set(report['outputs_retired']), {'4', '5'})
        self.assertEqual(set(report['results_retired']), {'4', '5'})

    def test_single_oversized_result_and_output_retire_after_exact_ack(self):
        self.finish('large', text='y' * 50000)
        report = self.trim({'output_bytes': 100, 'result_bytes': 100}, now=2)
        self.assertEqual(report['outputs_retired'], ['large'])
        self.assertEqual(report['results_retired'], ['large'])

    def test_completed_failed_command_can_retire_with_exact_ack(self):
        r.admit(self.db, self.job('one'));result = self.make_output('one')
        result.update(state='failed', exit_code=7)
        r.complete(self.db, 'one', result, quiescent=True, home=self.home)
        r.acknowledge(self.db, 'one', result, self.receipt('one', result), 'mac')
        report = self.trim(now=10**10)
        self.assertEqual(report['results_retired'], ['one'])
        self.assertEqual(self.row('one')[1]['exit_code'], 7)

    def test_crash_after_ack_before_cleanup_is_recoverable(self):
        self.finish('one')
        r.initialize(self.db);r.recover_running(self.db)
        self.assertEqual(self.trim()['results_retired'], ['one'])
        self.assertFalse(r.admit(self.db, self.job('one')))

    def test_crash_after_one_output_unlink_retries_remaining_copy(self):
        result = self.finish('one')
        def crash(*args):
            raise SimulatedCrash()
        with self.assertRaises(SimulatedCrash):
            self.trim(after_unlink=crash)
        self.assertFalse((self.folder('one') / 'stdout').exists())
        self.assertTrue((self.folder('one') / 'stderr').exists())
        self.assertEqual(self.row('one')[1], result)
        self.assertEqual(self.db.execute('SELECT outputs_retired FROM maintenance_output_retention').fetchone()[0], 0)
        self.assertIsNotNone(self.db.execute('SELECT output_manifest FROM maintenance_output_retention').fetchone()[0])
        self.assertEqual(self.trim()['outputs_retired'], ['one'])
        self.assertIsNone(self.db.execute('SELECT output_manifest FROM maintenance_output_retention').fetchone()[0])
        self.assertEqual(self.trim()['outputs_retired'], [])
        self.assertFalse((self.folder('one') / 'stderr').exists())

    def test_payload_tombstone_and_marker_are_one_transaction(self):
        result = self.finish('one')
        self.db.execute("CREATE TRIGGER fail_retire BEFORE UPDATE OF result_retired ON maintenance_output_retention BEGIN SELECT RAISE(ABORT,'crash'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.trim()
        self.assertEqual(self.row('one')[1], result)
        self.assertEqual(self.db.execute('SELECT result_retired FROM maintenance_output_retention').fetchone()[0], 0)
        self.db.execute('DROP TRIGGER fail_retire')
        self.assertEqual(self.trim()['results_retired'], ['one'])

    def test_active_outputs_or_incomplete_inspection_pin_payloads(self):
        result = self.finish('one')
        report = self.trim(active=lambda p: True)
        self.assertEqual(report['pinned'], ['one'])
        self.assertEqual(self.row('one')[1], result)
        self.assertTrue((self.folder('one') / 'stdout').exists())

    def test_symlinks_hardlinks_and_replaced_directories_are_never_followed(self):
        self.finish('one')
        out = self.folder('one') / 'stdout'
        original = out.read_bytes()
        user = self.home / 'user-file';user.write_bytes(original)
        out.unlink();out.symlink_to(user)
        self.assertEqual(self.trim()['pinned'], ['one'])
        self.assertEqual(user.read_bytes(), original)
        out.unlink();os.link(user, out)
        self.assertEqual(self.trim()['pinned'], ['one'])
        self.assertEqual(user.read_bytes(), original)

    def test_unpublished_or_modified_output_is_not_eligible_for_cleanup(self):
        result = self.finish('changed')
        out = self.folder('changed') / 'stdout'
        out.write_text('new bytes were never acknowledged')
        self.assertEqual(self.trim()['pinned'], ['changed'])
        self.assertEqual(self.row('changed')[1], result)
        r.admit(self.db, self.job('missing-payload'));self.make_output('missing-payload')
        failed = {'state': 'failed', 'error': 'Read failed before output publication'}
        r.complete(self.db, 'missing-payload', failed, quiescent=True, home=self.home)
        r.acknowledge(self.db, 'missing-payload', failed, self.receipt('missing-payload', failed), 'mac')
        self.trim()
        self.assertTrue((self.folder('missing-payload') / 'stdout').exists())
        self.assertIsNone(self.db.execute('SELECT completed FROM maintenance_output_retention WHERE id=?', ('missing-payload',)).fetchone()[0])

    def test_payload_changed_after_delivery_cannot_ack_or_compact(self):
        result = self.finish('one', ack=False)
        altered = dict(result, stdout='changed')
        self.db.execute('UPDATE commands SET result=? WHERE id=?', (json.dumps(altered), 'one'));self.db.commit()
        self.assertFalse(r.acknowledge(self.db, 'one', result, self.receipt('one', result), 'mac'))
        self.assertFalse(r.acknowledge(self.db, 'one', altered, self.receipt('one', altered), 'mac'))
        self.assertEqual(self.trim()['results_retired'], [])

    def test_process_group_observation_never_signals_or_assumes_descendants_finished(self):
        with mock.patch.object(r.os, 'killpg') as check:
            self.assertFalse(r.process_group_finished(123));check.assert_called_once_with(123, 0)
        for error, expected in [(ProcessLookupError(), True), (PermissionError(), False)]:
            with mock.patch.object(r.os, 'killpg', side_effect=error):
                self.assertEqual(r.process_group_finished(123), expected)

    def test_native_open_file_guard_ignores_only_own_read_verification_fds(self):
        for code, output, error, expected in [
            (1, b'', b'', False), (0, ('p%d\n' % os.getpid()).encode(), b'', False),
            (0, ('p%d\np999999\n' % os.getpid()).encode(), b'', True),
            (0, b'unknown', b'', True), (1, b'', b'warning', True), (2, b'', b'', True)]:
            process = mock.Mock(returncode=code);process.communicate.return_value = (output, error)
            with mock.patch.object(r.sys, 'platform', 'darwin'), mock.patch.object(r.subprocess, 'Popen', return_value=process):
                self.assertEqual(r.writers_present('/known/diagnostic'), expected)

    def test_retained_recent_outputs_do_not_spawn_native_inspections(self):
        self.finish('one')
        check = mock.Mock(side_effect=AssertionError('No cleanup needs inspection'))
        self.trim({}, now=2, active=check)
        check.assert_not_called()

    def test_staged_agent_execute_records_real_child_completion(self):
        import subprocess
        import threading
        source = (ROOT / 'deploy/maintenance-agent/agent.py').read_text()
        tree = ast.parse(source)
        functions = ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'execute'], type_ignores=[])
        scope = {'HOME': self.home, 'DB': self.db, 'LOCK': threading.RLock(),
                 'retention': r, 'hashlib': hashlib, 'json': json, 'subprocess': subprocess}
        exec(compile(functions, 'staged-maintenance-agent', 'exec'), scope)
        job = {'id': 'real-child', 'argv': ['/bin/sh', '-c', 'printf recent; printf diagnostic >&2'], 'cwd': None}
        self.assertTrue(r.admit(self.db, job))
        scope['execute'](job)
        state, result = self.row(job['id'])
        self.assertEqual(state, 'succeeded')
        self.assertEqual(result['stdout'], 'recent')
        self.assertEqual(result['stderr'], 'diagnostic')
        self.assertIsNotNone(self.db.execute('SELECT completed FROM maintenance_output_retention WHERE id=?', (job['id'],)).fetchone()[0])
        self.assertTrue(r.acknowledge(self.db, job['id'], result, self.receipt(job['id'], result), 'mac'))
        self.assertEqual(self.trim(now=10**10)['results_retired'], [job['id']])

    def test_policy_validation_and_agent_gate_removal_integration(self):
        for value in [{'unknown': 1}, {'output_count': -1}, {'output_bytes': True}]:
            with self.assertRaises(ValueError):
                r.policy(value)
        source = (ROOT / 'deploy/maintenance-agent/agent.py').read_text()
        tree = ast.parse(source)
        self.assertFalse(any(isinstance(n, ast.FunctionDef) and n.name == 'authorized' for n in tree.body))
        self.assertNotIn('authorized(job)', source)
        for call in ['retention.admit(DB, job)', 'retention.acknowledge(DB, identifier, sent, receipt, CONF[\'host\'])',
                     'retention.process_group_finished(process.pid)']:
            self.assertIn(call, source)
        self.assertIn("'Authorization':'Bearer ' + KEY", source)


if __name__ == '__main__':
    unittest.main()
