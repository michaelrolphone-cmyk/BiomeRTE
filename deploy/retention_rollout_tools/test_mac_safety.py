"""Local-only receipt/resource and verified Darwin wrapper regressions."""
import fcntl
import hashlib
import json
import os
import pathlib
import plistlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import mac_native as m
import terminal_safety as s


class MacReceiptSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.root = self.home / 'terminal-results'
        self.root.mkdir()
        db = sqlite3.connect(str(self.home / 'worker.sqlite'))
        db.execute('CREATE TABLE actions(id TEXT,state TEXT,result TEXT)')
        db.commit()
        db.close()
        self.snap = {'boot_id': None, 'processes': {
            10: {'pid': 10, 'ppid': 1, 'pgid': 10, 'started': 1, 'state': 'S'}}}

    def tearDown(self):
        self.tmp.cleanup()

    def receipt(self, op='op'):
        directory = self.root / hashlib.sha256(op.encode()).hexdigest()
        directory.mkdir(exist_ok=True)
        (directory / 'lock').touch()
        saved = {'operation': op, 'state': 'finished', 'started_at': 10,
                 'result': {'evidence': {'quiescent': True, 'duration_seconds': 2,
                                       'process_identity': {'pid': 20, 'pgid': 20}}}}
        (directory / 'receipt.json').write_text(json.dumps(saved))
        return directory

    def check(self):
        with patch.object(s, 'process_snapshot', return_value=self.snap):
            return s.inspect_local(str(self.home), 'Darwin', [10], self.snap)

    def assert_blocked(self, report):
        self.assertFalse(report['quiescent'], report)
        self.assertTrue(report['blocked'], report)

    def test_600_receipts_beneath_64_descriptor_limit(self):
        for index in range(600):
            self.receipt('op-' + str(index))
        code = ('import resource,json,terminal_safety as s,sys; '
                'soft,hard=resource.getrlimit(resource.RLIMIT_NOFILE); '
                'limit=64 if hard==resource.RLIM_INFINITY else min(64,hard); '
                'resource.setrlimit(resource.RLIMIT_NOFILE,(limit,hard)); '
                'snap=json.loads(sys.argv[2]); '
                'snap["processes"]={int(k):v for k,v in snap["processes"].items()}; '
                's.process_snapshot=lambda system:snap; '
                'print(json.dumps(s.inspect_local(sys.argv[1],"Darwin",[10],snap)))')
        output = subprocess.check_output([sys.executable, '-B', '-S', '-c', code,
                                          str(self.home), json.dumps(self.snap)],
                                         cwd=str(pathlib.Path(__file__).parent))
        report = json.loads(output.decode())
        self.assertTrue(report['quiescent'], report)
        self.assertEqual(report['checked_receipts'], 600)

    def after_first_pass(self, callback):
        original = s.mac_inventory
        count = [0]
        def inventory(root):
            count[0] += 1
            if count[0] == 2:
                callback()
            return original(root)
        return patch.object(s, 'mac_inventory', side_effect=inventory)

    def test_receipt_content_change_between_passes_blocks(self):
        directory = self.receipt()
        def change():
            path = directory / 'receipt.json'
            data = json.loads(path.read_text())
            data['state'] = 'running'
            path.write_text(json.dumps(data))
        with self.after_first_pass(change):
            report = self.check()
        self.assert_blocked(report)
        self.assertIn('receipt changed during inspection', str(report['blocked']))

    def test_same_content_atomic_replacement_blocks(self):
        directory = self.receipt()
        def replace():
            path = directory / 'receipt.json'
            tmp = directory / 'replacement'
            tmp.write_bytes(path.read_bytes())
            os.replace(str(tmp), str(path))
        with self.after_first_pass(replace):
            self.assert_blocked(self.check())

    def test_new_receipt_directory_blocks(self):
        self.receipt()
        with self.after_first_pass(lambda: self.receipt('new')):
            self.assert_blocked(self.check())

    def test_new_receipt_in_existing_directory_blocks(self):
        self.receipt()
        directory = self.root / hashlib.sha256(b'new').hexdigest()
        directory.mkdir()
        with self.after_first_pass(lambda: self.receipt('new')):
            self.assert_blocked(self.check())

    def test_deleted_receipt_blocks(self):
        directory = self.receipt()
        with self.after_first_pass(lambda: (directory / 'receipt.json').unlink()):
            self.assert_blocked(self.check())

    def test_replaced_lock_blocks(self):
        directory = self.receipt()
        def replace():
            (directory / 'lock').unlink()
            (directory / 'lock').touch()
        with self.after_first_pass(replace):
            self.assert_blocked(self.check())

    def test_adapter_taking_lock_between_passes_blocks(self):
        directory = self.receipt()
        with (directory / 'lock').open('rb') as lock:
            def acquire():
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.after_first_pass(acquire):
                report = self.check()
        self.assert_blocked(report)
        self.assertIn('receipt locked by active adapter', str(report['blocked']))

    def test_symlink_receipt_fails_closed(self):
        directory = self.receipt()
        path = directory / 'receipt.json'
        outside = self.home / 'outside.json'
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
        self.assert_blocked(self.check())

    def test_symlink_lock_fails_closed(self):
        directory = self.receipt()
        (directory / 'lock').unlink()
        (directory / 'lock').symlink_to(directory / 'receipt.json')
        self.assert_blocked(self.check())

    def test_read_error_fails_closed(self):
        self.receipt()
        with patch.object(s, 'mac_receipt', side_effect=OSError(24, 'Too many open files')):
            self.assert_blocked(self.check())

    def test_inspection_does_not_change_receipts_or_worker_ledger(self):
        self.receipt()
        paths = [p for p in self.home.rglob('*') if p.is_file()]
        before = {str(p): p.read_bytes() for p in paths}
        self.assertTrue(self.check()['quiescent'])
        self.assertEqual(before, {str(p): p.read_bytes() for p in paths})


class MacWrapperSafetyTests(unittest.TestCase):
    started = 'Sat Nov 7 15:42:15 2026'

    def worker(self):
        return m.PYTHON + ' ' + str(m.TERM / 'mac_provider.py') + ' worker'

    def wrapper(self, executable='caffeinate'):
        return executable + ' -is ' + self.worker()

    def row(self, pid, ppid, command, executable=None, uid=501, pgid=10, started=None):
        if executable is None:
            executable = '/usr/bin/caffeinate' if command.split()[0] in ('caffeinate', '/usr/bin/caffeinate') else m.PYTHON
        return '{} {} {} {} {} {} {}'.format(pid, ppid, pgid, uid,
                                              started or self.started, executable, command)

    def tree(self, rows):
        with patch.object(m.subprocess, 'check_output', return_value=('\n'.join(rows) + '\n').encode()):
            return m.verified_worker_tree(10)

    def test_both_caffeinate_command_spellings_in_both_topologies(self):
        for spelling in ('caffeinate', '/usr/bin/caffeinate'):
            for wrapper_root in (False, True):
                commands = [self.worker(), self.wrapper(spelling)]
                if wrapper_root:
                    commands.reverse()
                tree = self.tree([self.row(10, 1, commands[0]), self.row(11, 10, commands[1])])
                self.assertEqual(tree['roots'], [10, 11])
                self.assertEqual(tree['worker']['pid'], 11 if wrapper_root else 10)

    def test_plain_worker_has_no_exempt_children(self):
        tree = self.tree([self.row(10, 1, self.worker()), self.row(11, 10, '/bin/bash script', '/bin/bash')])
        self.assertEqual(tree['roots'], [10])

    def test_absolute_argv_with_basename_comm_in_both_topologies(self):
        for wrapper_root in (False, True):
            commands = [self.worker(), self.wrapper('/usr/bin/caffeinate')]
            executables = [m.PYTHON, 'caffeinate']
            if wrapper_root:
                commands.reverse()
                executables.reverse()
            tree = self.tree([self.row(10, 1, commands[0], executables[0]),
                              self.row(11, 10, commands[1], executables[1])])
            self.assertEqual(tree['roots'], [10, 11])
            self.assertEqual(tree['worker']['pid'], 11 if wrapper_root else 10)

    def test_both_basename_only_identities_do_not_exempt_watchdog(self):
        tree = self.tree([self.row(10, 1, self.worker()),
                          self.row(11, 10, self.wrapper(), 'caffeinate')])
        self.assertEqual(tree['roots'], [10])

    def test_native_truncated_comm_with_exact_absolute_argv_in_both_topologies(self):
        for wrapper_root in (False, True):
            commands=[self.worker(),self.wrapper('/usr/bin/caffeinate')]
            executables=['/Users/micahelby','/usr/bin/caffein']
            if wrapper_root:
                commands.reverse();executables.reverse()
            tree=self.tree([self.row(10,1,commands[0],executables[0]),
                            self.row(11,10,commands[1],executables[1])])
            self.assertEqual(tree['roots'],[10,11])
            self.assertEqual(tree['worker']['pid'],11 if wrapper_root else 10)

    def test_truncated_comm_requires_matching_prefix_width_and_absolute_argv(self):
        cases=[('/usr/bin/caffeiX','/usr/bin/caffeinate'),
               ('/tmp/bin/caffei','/usr/bin/caffeinate'),
               ('/usr/bin/caffei','/usr/bin/caffeinate'),
               ('/usr/bin/caffein','caffeinate')]
        for executable,argv0 in cases:
            tree=self.tree([self.row(10,1,self.worker()),
                            self.row(11,10,self.wrapper(argv0),executable)])
            self.assertEqual(tree['roots'],[10],(executable,argv0))

    def test_only_verified_watchdog_is_exempt(self):
        for changes in ({'uid': 502}, {'pgid': 11}, {'started': 'Sat Nov 7 15:45:15 2026'},
                        {'executable': '/tmp/caffeinate'}):
            tree = self.tree([self.row(10, 1, self.worker()), self.row(11, 10, self.wrapper(), **changes)])
            self.assertEqual(tree['roots'], [10], changes)

    def test_wrong_watchdog_arguments_remain_descendants(self):
        for command in ('caffeinate -is /bin/sleep 20',
                        self.wrapper().replace('-is', '-d'),
                        '/bin/echo /usr/bin/caffeinate -is ' + self.worker(),
                        self.wrapper() + ' --extra'):
            tree = self.tree([self.row(10, 1, self.worker()), self.row(11, 10, command, '/usr/bin/caffeinate')])
            self.assertEqual(tree['roots'], [10], command)

    def test_shell_substring_does_not_match_worker(self):
        with self.assertRaises(RuntimeError):
            self.tree([self.row(10, 1, '/bin/sh -c "' + self.worker() + '"', '/bin/sh')])

    def test_wrapper_root_with_unexpected_children_blocks(self):
        rows = [self.row(10, 1, self.wrapper()), self.row(11, 10, self.worker()),
                self.row(12, 10, '/bin/sleep 10', '/bin/sleep')]
        with self.assertRaises(RuntimeError):
            self.tree(rows)

    def test_local_audit_still_blocks_other_live_descendants(self):
        rows = [self.row(10, 1, self.worker()), self.row(11, 10, self.wrapper()),
                self.row(12, 10, '/bin/bash script', '/bin/bash')]
        snapshot = {'boot_id': None, 'processes': {
            pid: {'pid': pid, 'ppid': ppid, 'pgid': 10, 'started': 1, 'state': 'S'}
            for pid, ppid in ((10, 1), (11, 10), (12, 10), (13, 12))}}
        with tempfile.TemporaryDirectory() as d:
            home = pathlib.Path(d)
            (home / 'terminal-results').mkdir()
            db = sqlite3.connect(str(home / 'worker.sqlite'))
            db.execute('CREATE TABLE actions(id TEXT,state TEXT,result TEXT)')
            db.close()
            # Keep verified command paths fixed while directing only the audit ledger to a fixture.
            original = s.inspect_local
            def audit(_home, system, roots):
                with patch.object(s, 'process_snapshot', return_value=snapshot):
                    return original(str(home), system, roots, snapshot)
            with patch.object(m, 'inspect', return_value={'pid': 10}), \
                 patch.object(m.subprocess, 'check_output', return_value=('\n'.join(rows) + '\n').encode()), \
                 patch.object(s, 'inspect_local', side_effect=audit):
                report = m.local_safety(home)
        self.assertFalse(report['quiescent'])
        self.assertEqual({x['operation'] for x in report['blocked']},
                         {'worker-descendant:12', 'worker-descendant:13'})

    def restart(self, wrapper_root=False, quiescent=True, changed_child=False):
        commands = [self.worker(), self.wrapper()]
        if wrapper_root:
            commands.reverse()
        rows = [self.row(10, 1, commands[0]), self.row(11, 10, commands[1])]
        identities = {10: '501 ' + self.started + ' ' + commands[0],
                      11: '501 ' + self.started + ' ' + commands[1]}
        def output(argv):
            if '-axo' in argv:
                return ('\n'.join(rows) + '\n').encode()
            pid = int(argv[argv.index('-p') + 1])
            identity = identities[pid]
            if changed_child and pid == 11:
                identity = identity.replace('15:42:15', '15:42:16')
            return identity.encode()
        with tempfile.TemporaryDirectory() as d:
            home = pathlib.Path(d)
            plist = plistlib.dumps({'KeepAlive': True})
            (home / 'original-terminal.plist').write_bytes(plist)
            path = home / 'service.plist'
            path.write_bytes(plist)
            (home / 'before-terminal.json').write_text(json.dumps({'path': str(path)}))
            def service(_):
                return {'pid': 20 if kill.called else 10}
            with patch.object(m.sys, 'argv', ['mac_native.py', 'restart', '--bundle', d]), \
                 patch.object(m.os, 'getuid', return_value=501), \
                 patch.object(m, 'inspect', side_effect=service), \
                 patch.object(m, 'local_safety', return_value={'quiescent': quiescent}), \
                 patch.object(m.subprocess, 'check_output', side_effect=output), \
                 patch.object(m.os, 'kill') as kill:
                error = None
                try:
                    m.main()
                except RuntimeError as exc:
                    error = exc
                return kill.call_args_list, error

    def test_restart_signals_worker_not_watchdog_in_both_topologies(self):
        for wrapper_root in (False, True):
            calls, error = self.restart(wrapper_root)
            self.assertIsNone(error)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], (11 if wrapper_root else 10, m.signal.SIGTERM))

    def test_restart_cannot_signal_if_local_audit_blocks(self):
        calls, error = self.restart(quiescent=False)
        self.assertIsNotNone(error)
        self.assertEqual(calls, [])

    def test_restart_revalidates_worker_child_identity_before_signal(self):
        calls, error = self.restart(wrapper_root=True, changed_child=True)
        self.assertIsNotNone(error)
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()

class HelperHTTPFallbackTests(unittest.TestCase):
    def test_existing_remote_selected_without_persistent_attempt(self):
        import types
        fake=types.ModuleType('mac_provider');seen=[]
        class Remote:
            def __init__(self,manifest):seen.append((fake._persistent_after,manifest))
        fake.Remote=Remote
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d);manifest={'offers':[]};(root/'terminal-manifest.json').write_text(json.dumps(manifest))
            with patch.dict(sys.modules,{'mac_provider':fake}),patch.object(m,'TERM',root),patch.object(sys,'path',list(sys.path)):
                remote,actual=m.remote()
        self.assertIsInstance(remote,Remote);self.assertEqual(actual,manifest);self.assertEqual(seen,[(float('inf'),manifest)])
    def run_gate(self,failed):
        from unittest.mock import Mock
        from types import SimpleNamespace
        offer={'contract':'c','resource':'r','service':'terminal','capacity':16}
        with tempfile.TemporaryDirectory() as d:
            b=pathlib.Path(d);m.r.dump(b/'plan.json',{'phases':{'terminal':{}}})
            with patch.object(m,'audit_local'),patch.object(m,'remote',return_value=(SimpleNamespace(node='n'),{'offers':[offer]})),patch.object(m.g,'acquire') as acquire,patch.object(m.g,'restore') as restore,patch.object(m,'closed_probe',side_effect=RuntimeError('probe failed') if failed else None,return_value={'admission_closed':True}),patch.object(m.r,'terminal_gate_ready',return_value=True):
                if failed:
                    with self.assertRaises(RuntimeError):m.qualify_gate(b,'sha')
                else:m.qualify_gate(b,'sha')
                self.assertEqual(restore.call_count,1);self.assertEqual(acquire.call_args[1]['timeout'],30)
            self.assertFalse(m.r.read_json(b/'gate.json')['closed']);out=m.r.read_json(b/'gate-qualification.json');self.assertTrue(out['offers_restored']);self.assertEqual(out['passed'],not failed)
    def test_complete_gate_restores(self):self.run_gate(False)
    def test_failed_gate_restores(self):self.run_gate(True)
