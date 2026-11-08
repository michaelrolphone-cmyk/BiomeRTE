import errno
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import stat
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('service_log_retention', str(ROOT / 'deploy/service_log_retention.py'))
logs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(logs)


class LogRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(os.path.realpath(self.temp.name)) / 'home'
        self.home.mkdir(mode=0o700)
        self.plists = self.home / 'Library/LaunchAgents'
        self.plists.mkdir(parents=True, mode=0o700)
        self.paths = []
        self.sources = []
        self.new_service('org.biomerte.example')

    def new_service(self, label, combined=False):
        folder = self.home / '.local/share' / ('biome-' + label.rsplit('.', 1)[-1])
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        out, err = folder / 'service.log', folder / 'service-error.log'
        out.write_bytes(b'OLD STDOUT\n' * 8)
        err.write_bytes(b'OLD STDERR\n' * 8)
        path = self.plists / (label + '.plist')
        path.write_bytes(plistlib.dumps({'Label': label, 'ProgramArguments': ['/bin/no-service-is-run'],
            'StandardOutPath': str(out), 'StandardErrorPath': str(out if combined else err),
            'KeepAlive': True, 'RunAtLoad': True}))
        self.paths.append(str(path))
        self.sources.extend([out] if combined else [out, err])
        return out, err, path

    def manifest(self, cap=16, generations=2):
        value = logs.plan(str(self.home), self.paths, cap, generations)
        return value, logs.digest(logs.encoded(value))

    def apply(self, guard=lambda path, info: []):
        m, sha = self.manifest()
        return logs.apply(m, sha, writer_check=guard)

    def state(self, source):
        return self.home / '.local/share/biomerte-service-log-retention' / logs.digest(str(source).encode())

    def test_plan_is_read_only_deduplicates_shared_sinks_and_generalizes_labels(self):
        self.new_service('org.biomerte.not-a-hardcoded-provider', combined=True)
        before = {p: p.read_bytes() for p in self.sources}
        m, sha = self.manifest()
        self.assertEqual(len(m['targets']), 3)
        self.assertEqual(len(m['targets'][-1]['uses']), 2)
        self.assertFalse(Path(m['state_dir']).exists())
        self.assertEqual(before, {p: p.read_bytes() for p in self.sources})
        self.assertEqual(logs.validate_manifest(m, sha), m)

    def test_original_bytes_archived_and_inode_preserved(self):
        before = {p: (p.read_bytes(), p.stat().st_ino) for p in self.sources}
        report = self.apply()
        self.assertTrue(report['ok'])
        for p in self.sources:
            self.assertEqual(p.read_bytes(), b'')
            self.assertEqual(p.stat().st_ino, before[p][1])
            state = self.state(p)
            self.assertEqual((state / 'baseline.log').read_bytes(), before[p][0])
            meta = json.loads((state / 'baseline.json').read_text())
            self.assertEqual(meta['sha256'], logs.digest(before[p][0]))
            self.assertEqual((state / 'generation-0.log').read_bytes(), before[p][0][-16:])
            for entry in state.iterdir():
                self.assertEqual(stat.S_IMODE(entry.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)

    def test_fixed_generations_keep_latest_tails_without_orphan_growth(self):
        self.apply()
        original = [(self.state(p) / 'baseline.log').read_bytes() for p in self.sources]
        for i in range(25):
            for p in self.sources:
                p.write_bytes((str(i).zfill(2).encode() + b'X') * 15)
            self.assertTrue(self.apply()['ok'])
        for n, p in enumerate(self.sources):
            state = self.state(p)
            self.assertEqual((state / 'baseline.log').read_bytes(), original[n])
            generations = list(state.glob('generation-*.log'))
            self.assertEqual(len(generations), 2)
            self.assertTrue(all(x.stat().st_size == 16 for x in generations))
            self.assertEqual(set(x.read_bytes() for x in generations),
                             {((str(i).zfill(2).encode() + b'X') * 15)[-16:] for i in (23, 24)})
            self.assertEqual(len(list(state.iterdir())), 6)

    def test_within_limit_is_not_truncated_but_history_is_archived(self):
        m, sha = self.manifest(cap=1024)
        before = {p: p.read_bytes() for p in self.sources}
        report = logs.apply(m, sha, writer_check=mock.Mock(side_effect=AssertionError('No truncation needed')))
        self.assertTrue(report['ok'])
        self.assertTrue(all(x['state'] == 'within_limit' for x in report['results']))
        self.assertEqual(before, {p: p.read_bytes() for p in self.sources})

    def test_every_baseline_must_verify_before_any_log_is_truncated(self):
        before = {p: p.read_bytes() for p in self.sources}
        real = logs.verify_baseline
        count = [0]
        def fail_second(*args):
            count[0] += 1
            if count[0] == 2:
                raise ValueError('Injected archive failure')
            return real(*args)
        with mock.patch.object(logs, 'verify_baseline', side_effect=fail_second):
            with self.assertRaisesRegex(ValueError, 'Injected'):
                self.apply()
        self.assertEqual(before, {p: p.read_bytes() for p in self.sources})

    def test_unsafe_writer_fails_closed_preserving_original_history(self):
        before = {p: p.read_bytes() for p in self.sources}
        report = self.apply(mock.Mock(side_effect=ValueError('Active writer lacks O_APPEND')))
        self.assertFalse(report['ok'])
        self.assertTrue(all(x['state'] == 'blocked' for x in report['results']))
        self.assertEqual(before, {p: p.read_bytes() for p in self.sources})
        self.assertEqual(before, {p: (self.state(p) / 'baseline.log').read_bytes() for p in self.sources})
        self.assertTrue(all(not list(self.state(p).glob('generation-*.log')) for p in self.sources))

    def test_writers_rechecked_immediately_before_truncation(self):
        before = {p: p.read_bytes() for p in self.sources}
        calls = {}
        def guard(path, info):
            calls[path] = calls.get(path, 0) + 1
            if calls[path] == 3:
                raise ValueError('New nonappend writer')
            return []
        report = self.apply(guard)
        self.assertFalse(report['ok'])
        self.assertTrue(all(x == 3 for x in calls.values()))
        self.assertEqual(before, {p: p.read_bytes() for p in self.sources})

    def test_source_replacement_between_checks_does_not_touch_either_file(self):
        changed = []
        def guard(path, info):
            if path not in changed:
                os.rename(path, path + '.saved')
                Path(path).write_bytes(b'REPLACEMENT')
                changed.append(path)
            return []
        report = self.apply(guard)
        self.assertFalse(report['ok'])
        for p in self.sources:
            self.assertEqual(p.read_bytes(), b'REPLACEMENT')
            self.assertEqual(Path(str(p) + '.saved').read_bytes(), (self.state(p) / 'baseline.log').read_bytes())

    def test_corrupted_baseline_blocks_future_rotation(self):
        self.apply()
        for p in self.sources:
            p.write_bytes(b'NEW' * 20)
        state = self.state(self.sources[0])
        (state / 'baseline.log').write_bytes(b'broken')
        with self.assertRaisesRegex(ValueError, 'Baseline is not verified'):
            self.apply()
        self.assertTrue(all(p.read_bytes() == b'NEW' * 20 for p in self.sources))

    def test_baseline_pending_commit_does_not_authorize_truncation(self):
        before = {p: p.read_bytes() for p in self.sources}
        actual = logs.atomic_private
        def fail_metadata(directory, name, value, uid):
            if name == 'baseline.json':
                raise OSError(errno.ENOSPC, 'disk full')
            return actual(directory, name, value, uid)
        with mock.patch.object(logs, 'atomic_private', side_effect=fail_metadata):
            with self.assertRaises(OSError):
                self.apply()
        self.assertEqual(before, {p: p.read_bytes() for p in self.sources})
        with self.assertRaisesRegex(ValueError, 'Uncommitted baseline'):
            self.apply()
        self.assertEqual(before, {p: p.read_bytes() for p in self.sources})

    def test_symlink_source_fails_closed(self):
        p = self.sources[0]
        original = p.read_bytes()
        elsewhere = self.home / 'user-file'
        elsewhere.write_bytes(original)
        p.unlink()
        p.symlink_to(elsewhere)
        with self.assertRaises(OSError):
            self.apply()
        self.assertEqual(elsewhere.read_bytes(), original)

    def test_symlink_directory_fails_closed(self):
        folder = self.sources[0].parent
        moved = folder.with_name('real')
        folder.rename(moved)
        folder.symlink_to(moved)
        with self.assertRaises(OSError):
            self.apply()
        self.assertTrue(all(p.read_bytes().startswith(b'OLD') for p in self.sources))

    def test_hardlinked_source_fails_closed(self):
        p = self.sources[0]
        os.link(p, self.home / 'user-file')
        with self.assertRaisesRegex(ValueError, 'singly linked'):
            self.apply()
        self.assertTrue(p.read_bytes().startswith(b'OLD'))

    def test_group_writable_source_fails_closed(self):
        self.sources[0].chmod(0o660)
        with self.assertRaisesRegex(ValueError, 'writable by others'):
            self.apply()
        self.assertTrue(self.sources[0].read_bytes().startswith(b'OLD'))

    def test_wrong_owner_rejected(self):
        fd = os.open(self.sources[0], os.O_RDONLY)
        try:
            with self.assertRaisesRegex(ValueError, 'owned by the service user'):
                logs.regular(fd, os.getuid() + 1)
        finally:
            os.close(fd)

    def test_changed_plist_and_wrong_manifest_digest_fail_before_writes(self):
        m, sha = self.manifest()
        with self.assertRaisesRegex(ValueError, 'digest differs'):
            logs.apply(m, 'wrong', writer_check=lambda *a: [])
        d = plistlib.loads(Path(self.paths[0]).read_bytes())
        d['KeepAlive'] = False
        Path(self.paths[0]).write_bytes(plistlib.dumps(d))
        with self.assertRaisesRegex(ValueError, 'configuration or manifest'):
            logs.apply(m, sha, writer_check=lambda *a: [])
        self.assertFalse(Path(m['state_dir']).exists())

    def test_scoped_paths_exclude_user_files_commands_records_and_retention_state(self):
        d = plistlib.loads(Path(self.paths[0]).read_bytes())
        for path in [self.home / 'user.log', self.home / '.local/share/biome-example/commands/a.log',
                     self.home / '.local/share/biome-example/records.sqlite',
                     self.home / '.local/share/biomerte-service-log-retention/baseline.log']:
            d['StandardOutPath'] = str(path)
            Path(self.paths[0]).write_bytes(plistlib.dumps(d))
            with self.assertRaises(ValueError):
                self.manifest()

    def test_other_user_services_including_vnc_are_not_admitted(self):
        _, _, path = self.new_service('com.example.vnc')
        m, _ = self.manifest()
        self.assertEqual(len(m['services']), 1)
        self.assertTrue(all('example' in x['path'] and 'vnc' not in x['path'] for x in m['targets']))

    def test_maintenance_plain_logs_are_supported_without_command_outputs(self):
        self.new_service('org.biomerte.maintenance')
        command = self.home / '.local/share/biome-maintenance/commands/active/stdout'
        command.parent.mkdir(parents=True)
        command.write_bytes(b'active VNC command output must remain')
        self.assertTrue(self.apply()['ok'])
        self.assertEqual(command.read_bytes(), b'active VNC command output must remain')

    def test_lock_prevents_competing_rotators(self):
        m, sha = self.manifest()
        state = Path(m['state_dir'])
        state.mkdir(parents=True, mode=0o700)
        fd = os.open(state / 'lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                logs.apply(m, sha, writer_check=lambda *a: [])
            self.assertFalse((state / 'manifest.json').exists())
        finally:
            os.close(fd)

    def test_large_line_is_streamed_and_rolling_copy_is_bounded(self):
        self.sources[0].write_bytes(b'x' * (4 * 1024 * 1024) + b'end')
        old_read = os.read
        sizes = []
        def checked_read(fd, size):
            sizes.append(size)
            return old_read(fd, size)
        with mock.patch.object(logs.os, 'read', side_effect=checked_read):
            self.assertTrue(self.apply()['ok'])
        self.assertLessEqual(max(sizes), logs.CHUNK)
        state = self.state(self.sources[0])
        self.assertEqual((state / 'baseline.log').stat().st_size, 4 * 1024 * 1024 + 3)
        self.assertEqual((state / 'generation-0.log').read_bytes(), b'x' * 13 + b'end')

    def lsof_output(self, flags='R,W,AP;SH', fd='1u', inode=None):
        p = self.sources[0]
        info = p.stat()
        return ('COMMAND PID USER FD TYPE FILE-FLAG DEVICE SIZE/OFF NODE NAME\n'
                'Python 123 owner {} REG {} {},{} 100 {} {}\n').format(
                    fd, flags, os.major(info.st_dev), os.minor(info.st_dev),
                    inode or info.st_ino, p)

    def test_rotator_self_fd_is_append_and_fd_budget_does_not_scale(self):
        import resource
        for n in range(60):
            self.new_service('org.biomerte.synthetic-{}'.format(n), combined=True)
        original_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
        calls = []
        def real_fd_guard(path, info):
            found = []
            for fd in range(64):
                try:
                    current = os.fstat(fd)
                    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
                except OSError:
                    continue
                if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
                    if flags & os.O_ACCMODE in (os.O_WRONLY, os.O_RDWR):
                        self.assertTrue(flags & os.O_APPEND, 'Rotator would reject its own descriptor')
                        found.append(fd)
            self.assertTrue(found)
            calls.append(path)
            return []
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (64, original_limit[1]))
            report = self.apply(real_fd_guard)
        finally:
            resource.setrlimit(resource.RLIMIT_NOFILE, original_limit)
        self.assertTrue(report['ok'])
        self.assertEqual(len(report['results']), 62)
        self.assertEqual(len(calls), 3 * 62)

    def test_lsof_parser_requires_append_for_every_writable_descriptor(self):
        p = self.sources[0]
        text = self.lsof_output()
        self.assertEqual(logs.parse_writers(text, str(p), p.stat()), [{'pid': 123, 'fd': 1}])
        for bad in [self.lsof_output('R,W'), self.lsof_output(fd='mem'),
                    self.lsof_output(inode=p.stat().st_ino + 1), 'different output']:
            with self.assertRaises(ValueError):
                logs.parse_writers(bad, str(p), p.stat())
        self.assertEqual(logs.parse_writers(self.lsof_output('R', '3r'), str(p), p.stat()), [])

    def test_lsof_failures_and_incomplete_visibility_fail_closed(self):
        p = self.sources[0]
        for code, out, err in [(1, b'', b'permission denied'), (0, self.lsof_output().encode(), b'warning'),
                               (2, b'', b''), (0, b'', b'')]:
            process = mock.Mock(returncode=code)
            process.communicate.return_value = (out, err)
            with mock.patch.object(logs.sys, 'platform', 'darwin'), \
                 mock.patch.object(logs.subprocess, 'Popen', return_value=process):
                with self.assertRaises(ValueError):
                    logs.inspect_writers(str(p), p.stat())
        process = mock.Mock(returncode=1)
        process.communicate.return_value = (b'', b'')
        with mock.patch.object(logs.sys, 'platform', 'darwin'), \
             mock.patch.object(logs.subprocess, 'Popen', return_value=process):
            with self.assertRaises(ValueError):
                logs.inspect_writers(str(p), p.stat())

    def test_native_writer_guard_must_see_its_own_append_descriptor(self):
        p = self.sources[0]
        process = mock.Mock(returncode=0)
        process.communicate.return_value = (self.lsof_output().replace('Python 123 ', 'Python %d ' % os.getpid()).encode(), b'')
        with mock.patch.object(logs.sys, 'platform', 'darwin'), \
             mock.patch.object(logs.subprocess, 'Popen', return_value=process):
            self.assertEqual(logs.inspect_writers(str(p), p.stat())[0]['pid'], os.getpid())
        process.communicate.return_value = (self.lsof_output().encode(), b'')
        with mock.patch.object(logs.sys, 'platform', 'darwin'), \
             mock.patch.object(logs.subprocess, 'Popen', return_value=process):
            with self.assertRaisesRegex(ValueError, 'cannot see the rotator'):
                logs.inspect_writers(str(p), p.stat())


if __name__ == '__main__':
    unittest.main()
