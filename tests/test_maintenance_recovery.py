"""Offline qualification of independent maintenance execution and local repair.

CI discovers this module on Linux and macOS. Launchd and command processes are
simulated; no controller, enrollment, network or physical service is contacted.
"""
import ast
import base64
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
RECOVERY = ROOT/'gateway'/'recovery'
AGENT = RECOVERY/'maintenance_agent.py'
REPAIR = RECOVERY/'Repair-Biome-Maintenance.command'


def repair_scope():
    text = REPAIR.read_text()
    code = text.split("<<'BIOME_MAINTENANCE_REPAIR'\n", 1)[1].rsplit('\nBIOME_MAINTENANCE_REPAIR', 1)[0]
    scope = {'__name__': 'maintenance_repair_test'}
    exec(compile(code, str(REPAIR), 'exec'), scope)
    return scope


class InlineThread:
    def __init__(self, target, args, daemon):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


class MaintenanceAgentTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.home = Path(self.folder.name)
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE commands(id TEXT PRIMARY KEY,state TEXT,result TEXT)')
        self.popen = Mock(side_effect=self.process)
        tree = ast.parse(AGENT.read_text())
        functions = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef)
                                     and n.name in ('execute', 'save_result', 'start_job')], type_ignores=[])
        self.scope = dict(HOME=self.home, DB=self.db, LOCK=threading.RLock(), hashlib=hashlib,
                          json=json, subprocess=SimpleNamespace(Popen=self.popen),
                          threading=SimpleNamespace(Thread=InlineThread))
        exec(compile(functions, str(AGENT), 'exec'), self.scope)

    def tearDown(self):
        self.db.close()
        self.folder.cleanup()

    def process(self, argv, **options):
        self.assertEqual(argv, ['/bin/echo', 'test'])
        self.assertTrue(options['start_new_session'])
        options['stdout'].write(b'test\n')
        return SimpleNamespace(pid=12345, wait=lambda: 0)

    def result(self, identifier='job'):
        state, result = self.db.execute('SELECT state,result FROM commands WHERE id=?', (identifier,)).fetchone()
        return state, json.loads(result)

    def test_owner_admitted_unsigned_command_executes_once_and_retains_output(self):
        job = {'id': 'job', 'argv': ['/bin/echo', 'test'], 'cwd': None}
        self.assertTrue(self.scope['start_job'](job))
        self.assertFalse(self.scope['start_job'](job))
        self.assertEqual(self.popen.call_count, 1)
        state, result = self.result()
        self.assertEqual(state, 'succeeded')
        self.assertEqual(result['stdout'], 'test\n')
        self.assertEqual(result['stderr'], '')
        self.assertEqual(result['exit_code'], 0)

    def test_command_directory_failure_gets_durable_result_without_starting_process(self):
        with patch.object(Path, 'mkdir', side_effect=PermissionError('read-only test directory')):
            self.scope['start_job']({'id': 'job', 'argv': ['/bin/echo', 'test']})
        state, result = self.result()
        self.assertEqual(state, 'failed')
        self.assertIn('PermissionError', result['error'])
        self.assertIn('output_directory', result)
        self.popen.assert_not_called()
        self.assertFalse(self.scope['start_job']({'id': 'job', 'argv': ['/bin/echo', 'test']}))

    def test_thread_start_failure_gets_durable_result_and_is_not_replayed(self):
        thread = Mock()
        thread.start.side_effect = RuntimeError('thread unavailable')
        self.scope['threading'] = SimpleNamespace(Thread=Mock(return_value=thread))
        job = {'id': 'job', 'argv': ['/bin/echo', 'test']}
        self.assertFalse(self.scope['start_job'](job))
        self.assertFalse(self.scope['start_job'](job))
        state, result = self.result()
        self.assertEqual(state, 'failed')
        self.assertIn('worker could not start', result['error'])
        self.popen.assert_not_called()
        thread.start.assert_called_once()

    def test_existing_uncertain_and_reported_commands_are_never_replayed(self):
        for state in ('uncertain', 'reported', 'running'):
            self.db.execute('INSERT INTO commands VALUES(?,?,?)', (state, state, '{}'))
            self.assertFalse(self.scope['start_job']({'id': state, 'argv': ['/bin/echo', 'test']}))
        self.popen.assert_not_called()

    def test_restart_marks_only_running_rows_uncertain_without_launching_a_command(self):
        (self.home/'config.json').write_text(json.dumps({'origin': 'https://example.invalid', 'host': 'mac'}))
        (self.home/'device.key').write_text('a'*64)
        path = self.home/'ledger.sqlite'
        with sqlite3.connect(str(path)) as db:
            db.execute('CREATE TABLE commands(id TEXT PRIMARY KEY,state TEXT,result TEXT)')
            for state in ('running', 'uncertain', 'reported', 'succeeded'):
                db.execute('INSERT INTO commands VALUES(?,?,?)', (state, state, json.dumps({'original': state})))
        db.close()
        tree = ast.parse(AGENT.read_text())
        tree.body = [node for node in tree.body if not isinstance(node, ast.While)]
        scope = {'__name__': 'maintenance_restart_test'}
        old_umask = os.umask(0o077)
        try:
            with patch.object(sys, 'argv', ['maintenance_agent.py', str(self.home)]), patch.object(subprocess, 'Popen') as child:
                exec(compile(tree, str(AGENT), 'exec'), scope)
                rows = dict((identifier, (state, result)) for identifier, state, result in
                            scope['DB'].execute('SELECT id,state,result FROM commands'))
                self.assertEqual(rows['running'][0], 'uncertain')
                self.assertIn('restarted', json.loads(rows['running'][1])['reason'])
                for state in ('uncertain', 'reported', 'succeeded'):
                    self.assertEqual(rows[state], (state, json.dumps({'original': state})))
                child.assert_not_called()
        finally:
            if 'DB' in scope:
                scope['DB'].close()
            os.umask(old_umask)


class MaintenanceRepairTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.home = Path(self.folder.name)
        self.plist = self.home/'org.biomerte.maintenance.plist'
        self.agent = self.home/'agent.py'
        self.agent.write_bytes(b'# previous agent\n')
        (self.home/'config.json').write_text(json.dumps({'origin': 'https://example.invalid', 'host': 'enrolled-mac'}))
        (self.home/'device.key').write_text('b'*64)
        with sqlite3.connect(str(self.home/'ledger.sqlite')) as db:
            db.execute('CREATE TABLE commands(id TEXT PRIMARY KEY,state TEXT,result TEXT)')
            db.execute("INSERT INTO commands VALUES('old','uncertain','{}')")
        db.close()
        self.service = {'Label': 'org.biomerte.maintenance',
                        'ProgramArguments': [sys.executable, '-S', str(self.agent), str(self.home)],
                        'WorkingDirectory': str(self.home), 'KeepAlive': True, 'RunAtLoad': True}
        self.plist.write_bytes(plistlib.dumps(self.service))
        self.scope = repair_scope()
        self.run = Mock(return_value=SimpleNamespace(returncode=0))
        self.old_umask = os.umask(0o077)

    def tearDown(self):
        os.umask(self.old_umask)
        self.folder.cleanup()

    def preserved(self):
        return {name: (self.home/name).read_bytes() for name in
                ('org.biomerte.maintenance.plist', 'config.json', 'device.key', 'ledger.sqlite')}

    def test_installer_and_repair_embed_exact_canonical_agent_with_verified_pin(self):
        canonical = AGENT.read_bytes()
        installer = (RECOVERY/'Install-Biome-Maintenance.command').read_text()
        encoded = re.search(r"/usr/bin/python3 -S - '([A-Za-z0-9+/=]+)'", installer).group(1)
        self.assertEqual(base64.b64decode(encoded, validate=True), canonical)
        self.assertEqual(base64.b64decode(self.scope['AGENT_BASE64'], validate=True), canonical)
        self.assertEqual(self.scope['AGENT_SHA256'], hashlib.sha256(canonical).hexdigest())
        compile(canonical, str(AGENT), 'exec')

    def test_repairs_existing_enrollment_atomically_and_restarts_only_maintenance(self):
        before = self.preserved()
        previous = self.agent.read_bytes()
        result = self.scope['repair'](self.plist, run=self.run)
        self.assertEqual(self.agent.read_bytes(), AGENT.read_bytes())
        self.assertEqual(Path(result['previous_agent']).read_bytes(), previous)
        self.assertEqual(self.preserved(), before)
        target = 'gui/' + str(os.getuid()) + '/org.biomerte.maintenance'
        self.assertEqual([call.args[0] for call in self.run.call_args_list],
                         [['/bin/launchctl', 'print', target],
                          ['/bin/launchctl', 'kickstart', '-k', target]])
        self.assertEqual(list(self.home.glob('.maintenance-agent-*')), [])

    def test_missing_enrollment_and_wrong_service_fail_without_replacement(self):
        before = self.agent.read_bytes()
        key = self.home/'device.key'
        saved = key.read_bytes()
        key.unlink()
        with self.assertRaisesRegex(RuntimeError, 'device.key'):
            self.scope['repair'](self.plist, run=self.run)
        key.write_bytes(saved)
        self.service['Label'] = 'org.biomerte.controller'
        self.plist.write_bytes(plistlib.dumps(self.service))
        with self.assertRaisesRegex(RuntimeError, 'unsupported command'):
            self.scope['repair'](self.plist, run=self.run)
        self.assertEqual(self.agent.read_bytes(), before)
        self.run.assert_not_called()

    def test_bad_payload_pin_and_unloaded_service_fail_before_replacement(self):
        before = self.agent.read_bytes()
        digest = self.scope['AGENT_SHA256']
        self.scope['AGENT_SHA256'] = '0'*64
        with self.assertRaisesRegex(RuntimeError, 'checksum'):
            self.scope['repair'](self.plist, run=self.run)
        self.run.assert_not_called()
        self.scope['AGENT_SHA256'] = digest
        self.run.return_value.returncode = 1
        with self.assertRaisesRegex(RuntimeError, 'not loaded'):
            self.scope['repair'](self.plist, run=self.run)
        self.assertEqual(self.agent.read_bytes(), before)
        self.assertEqual(list(self.home.glob('agent.py.before-repair-*')), [])

    def test_failed_atomic_replace_preserves_previous_agent_and_enrollment(self):
        before = self.preserved()
        previous = self.agent.read_bytes()
        with patch.object(self.scope['os'], 'replace', side_effect=OSError('replace unavailable')):
            with self.assertRaisesRegex(OSError, 'replace unavailable'):
                self.scope['repair'](self.plist, run=self.run)
        self.assertEqual(self.agent.read_bytes(), previous)
        self.assertEqual(self.preserved(), before)
        self.assertEqual(self.run.call_count, 1)
        self.assertEqual(list(self.home.glob('.maintenance-agent-*')), [])

    def test_restart_failure_retains_backup_and_does_not_claim_recovery(self):
        self.run.side_effect = [SimpleNamespace(returncode=0), SimpleNamespace(returncode=1)]
        previous = self.agent.read_bytes()
        before = self.preserved()
        with self.assertRaisesRegex(RuntimeError, 'installed, but maintenance restart failed'):
            self.scope['repair'](self.plist, run=self.run)
        self.assertEqual(self.agent.read_bytes(), AGENT.read_bytes())
        backups = list(self.home.glob('agent.py.before-repair-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), previous)
        self.assertEqual(self.preserved(), before)


if __name__ == '__main__':
    unittest.main()
