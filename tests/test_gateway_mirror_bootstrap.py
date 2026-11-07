"""Disposable-file tests only: never inspect or control a real service."""
import errno
import hashlib
import importlib.machinery
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, Mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
helper = importlib.machinery.SourceFileLoader(
    'gateway_mirror_bootstrap_test', os.path.join(ROOT, 'deploy', 'bootstrap-gateway-mirror.py')).load_module()


def make_db(path, seq, anchor):
    db = sqlite3.connect(path)
    db.executescript('CREATE TABLE records(kind TEXT,id TEXT,value TEXT,PRIMARY KEY(kind,id));'
                     'CREATE TABLE position(id INTEGER PRIMARY KEY,seq INTEGER,hash TEXT);')
    db.execute('INSERT INTO position VALUES(1,?,?)', (seq, anchor))
    db.execute('INSERT INTO records VALUES(?,?,?)', ('test', 'item', json.dumps({'seq': seq})))
    db.commit()
    db.close()


class Fake(helper.Bootstrap):
    def preflight(self):
        self.calls = []
        self.config = {'listen': '127.0.0.1', 'port': 8740}
        self.previously_active = getattr(self, 'initial_active', [helper.CONTROLLER, helper.GATEWAY])
        self.protected = {os.path.join(self.home, p): helper.file_digest(os.path.join(self.home, p))
                          for p in ('node.json', 'node.key', 'local.sqlite', 'worker.sqlite')}
        self.probe_count = 0

    def progress(self, phase):
        self.phase = phase

    def probe(self):
        self.probe_count += 1
        return {'listening': self.probe_count == getattr(self, 'listens_at', -1), 'healthy': False}

    def verify_remote(self):
        self.calls.append('remote')
        if getattr(self, 'bad_remote', False):
            raise helper.Refused('Authoritative snapshot anchor mismatch')

    def unchanged(self):
        for path, digest in self.protected.items():
            helper.require(helper.file_digest(path) == digest, 'Protected file changed')

    def stop(self):
        self.calls.append('stop')
        if getattr(self, 'bad_stop', False):
            raise helper.Refused('Partial stop')

    def quiescent(self):
        self.calls.append('quiescent')
        if getattr(self, 'owners', False):
            raise helper.Refused('Owner remains')

    def start(self):
        self.calls.append('start')
        if getattr(self, 'bad_start', False):
            raise KeyboardInterrupt()

    def restore_services(self):
        self.calls.append(('restore', tuple(self.previously_active)))

    def wait_health(self):
        return {'listening': True, 'healthy': not getattr(self, 'not_ready', False)}


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, 'home')
        os.mkdir(self.home)
        self.snapshot = os.path.join(self.tmp.name, 'snapshot.sqlite')
        make_db(self.snapshot, 20, 'a' * 64)
        self.manifest = {'site': 'test-site', 'gateway_origin': 'https://example.invalid',
                         'source_node': 'b' * 64, 'sequence': 20, 'hash': 'a' * 64,
                         'snapshot_sha256': helper.file_digest(self.snapshot),
                         'snapshot_bytes': os.stat(self.snapshot).st_size}
        self.job = Fake(self.manifest, '', self.home, ROOT, '', 'test-host', 'test-user', self.snapshot)
        make_db(self.job.db_path, 3, 'c' * 64)
        self.original = helper.file_digest(self.job.db_path)
        for name in ('node.json', 'node.key', 'local.sqlite', 'worker.sqlite'):
            with open(os.path.join(self.home, name), 'wb') as stream:
                stream.write(('untouched-' + name).encode('ascii'))

    def original_restored(self):
        self.assertEqual(helper.file_digest(self.job.db_path), self.original)
        self.job.unchanged()
        self.assertFalse(os.path.exists(self.job.stage))

    def test_local_snapshot_success_preserves_original_and_identity(self):
        result = self.job.run()
        self.assertEqual(result['state'], 'installed')
        self.assertEqual(helper.position(self.job.db_path), (20, 'a' * 64))
        self.assertEqual(helper.file_digest(os.path.join(result['backup'], os.path.basename(self.job.db_path))), self.original)
        self.assertEqual(os.stat(result['backup']).st_mode & 0o777, 0o700)
        self.job.unchanged()
        self.assertEqual(helper.file_digest(self.snapshot), self.manifest['snapshot_sha256'])

    def test_listener_before_preparation_does_nothing(self):
        self.job.listens_at = 1
        self.assertEqual(self.job.run()['state'], 'unchanged')
        self.assertIsNone(self.job.stage)
        self.assertEqual(self.job.calls, [])

    def test_already_caught_up_before_preparation_does_nothing(self):
        with patch.object(helper, 'position', return_value=(20, 'a' * 64)):
            self.assertEqual(self.job.run()['reason'], 'mirror_already_caught_up')
        self.assertIsNone(self.job.stage)
        self.assertEqual(self.job.calls, [])

    def test_catches_up_during_stop_without_replacing_cache(self):
        real_position = helper.position
        queries = []

        def query(path):
            if path == self.job.db_path:
                queries.append(path)
                return (20, 'a' * 64) if len(queries) >= 3 else (3, 'c' * 64)
            return real_position(path)

        with patch.object(helper, 'position', side_effect=query):
            self.assertEqual(self.job.run()['state'], 'unchanged')
        self.original_restored()

    def test_listener_after_download_avoids_stopping(self):
        self.job.listens_at = 2
        self.assertEqual(self.job.run()['state'], 'unchanged')
        self.assertNotIn('stop', self.job.calls)
        self.original_restored()

    def test_listener_after_stop_restores_original_service_states(self):
        self.job.listens_at = 3
        self.job.initial_active = [helper.CONTROLLER]
        self.assertEqual(self.job.run()['state'], 'unchanged')
        self.assertIn(('restore', (helper.CONTROLLER,)), self.job.calls)
        self.original_restored()

    def test_inactive_services_remain_inactive_on_preinstall_failure(self):
        self.job.initial_active = []
        self.job.bad_stop = True
        with self.assertRaises(helper.Refused):
            self.job.run()
        self.assertIn(('restore', ()), self.job.calls)
        self.original_restored()

    def test_partial_stop_failure_restarts_previously_active_units(self):
        self.job.bad_stop = True
        with self.assertRaises(helper.Refused):
            self.job.run()
        self.assertIn(('restore', (helper.CONTROLLER, helper.GATEWAY)), self.job.calls)
        self.original_restored()

    def test_checksum_failure_never_stops_services(self):
        self.manifest['snapshot_sha256'] = '0' * 64
        with self.assertRaises(helper.Refused):
            self.job.run()
        self.assertNotIn('stop', self.job.calls)
        self.original_restored()

    def test_remote_anchor_failure_never_stops_services(self):
        self.job.bad_remote = True
        with self.assertRaises(helper.Refused):
            self.job.run()
        self.assertNotIn('stop', self.job.calls)
        self.original_restored()

    def test_remote_validation_only_reads_and_checks_anchor(self):
        from biomerte import store
        entry = {'seq': 20, 'prev': 'c' * 64, 'changes': []}
        self.manifest['hash'] = store.digest(entry)
        key = os.path.join(self.tmp.name, 'test-only-key')
        with open(key, 'w') as stream:
            stream.write('test-only')
        self.job.config = {'site': self.manifest['site'], 'gateway_journal': {
            'origin': 'https://example.invalid', 'key_file': key}}
        requests = []

        def opened(request, timeout):
            body = json.loads(request.data.decode('utf-8'))
            requests.append((request.full_url, body))
            if request.full_url.endswith('/status'):
                value = {'site': self.manifest['site']}
            elif body['key'] == 'head':
                value = {'value': {'seq': 20, 'hash': self.manifest['hash']}, 'version': '20'}
            else:
                value = {'encoded': store.encode(entry)}
            return io.BytesIO(json.dumps(value).encode('utf-8'))

        with patch.object(helper, 'build_opener', return_value=Mock(open=opened)) as opener:
            helper.Bootstrap.verify_remote(self.job)
        self.assertEqual([url.rsplit('/', 1)[1] for url, body in requests], ['status', 'read', 'read'])
        self.assertEqual(requests[-1][1], {'key': 'log/00000000000000000020'})
        for call in opener.call_args_list:
            self.assertIn(helper.NoRedirect, call[0])

    def test_low_space_fails_before_snapshot_or_stop(self):
        with patch.object(helper.shutil, 'disk_usage', return_value=Mock(free=0)):
            with self.assertRaises(helper.Refused):
                self.job.run()
        self.assertIsNone(self.job.stage)
        self.assertEqual(self.job.calls, [])

    def test_owners_prevent_cache_replacement(self):
        self.job.owners = True
        with self.assertRaises(helper.Refused):
            self.job.run()
        self.original_restored()

    def test_interrupt_after_each_successful_move_restores_all_files(self):
        for index in range(3):
            with self.subTest(index=index):
                for path in self.job.paths[1:]:
                    with open(path, 'wb') as stream:
                        stream.write(('sidecar-' + path).encode())
                originals = {p: helper.file_digest(p) for p in self.job.paths}
                real_position, real_rename = helper.position, os.rename
                moved = []

                def query(path):
                    return (3, 'c' * 64) if path == self.job.db_path else real_position(path)

                def rename(source, target):
                    real_rename(source, target)
                    if source in self.job.paths and 'previous-cache' in target:
                        moved.append(source)
                        if len(moved) == index + 1:
                            raise KeyboardInterrupt()

                with patch.object(helper, 'position', side_effect=query), patch.object(helper.os, 'rename', side_effect=rename):
                    with self.assertRaises(helper.Refused):
                        self.job.run()
                for path, digest in originals.items():
                    self.assertEqual(helper.file_digest(path), digest)
                self.original_restored()
                for path in self.job.paths[1:]:
                    os.unlink(path)

    def test_interrupt_after_link_before_bookkeeping_restores_original(self):
        real_link = os.link

        def interrupted_link(source, target):
            real_link(source, target)
            raise KeyboardInterrupt()

        with patch.object(helper.os, 'link', side_effect=interrupted_link):
            with self.assertRaises(helper.Refused):
                self.job.run()
        self.original_restored()

    def test_interrupt_at_replacement_start_never_rolls_back(self):
        self.job.bad_start = True
        with self.assertRaises(helper.Refused) as caught:
            self.job.run()
        self.assertIn('Replacement start attempted', str(caught.exception))
        self.assertEqual(helper.position(self.job.db_path)[0], 20)
        self.assertTrue(os.path.isfile(os.path.join(self.job.backup, os.path.basename(self.job.db_path))))
        self.assertFalse(any(isinstance(x, tuple) for x in self.job.calls))

    def test_unaccounted_sidecar_blocks_restore_and_retains_backup(self):
        real_link = os.link

        def raced_link(source, target):
            real_link(source, target)
            with open(target + '-wal', 'wb') as stream:
                stream.write(b'unaccounted')

        with patch.object(helper.os, 'link', side_effect=raced_link):
            with self.assertRaises(helper.Refused) as caught:
                self.job.run()
        self.assertIn('Recovery incomplete', str(caught.exception))
        self.assertTrue(os.path.isfile(os.path.join(self.job.backup, os.path.basename(self.job.db_path))))
        self.assertNotIn('start', self.job.calls)
        self.assertFalse(any(isinstance(x, tuple) for x in self.job.calls))

    def test_not_ready_after_start_retains_new_cache_and_backup(self):
        self.job.not_ready = True
        self.assertEqual(self.job.run()['state'], 'installed_not_ready')
        self.assertEqual(helper.position(self.job.db_path)[0], 20)
        self.assertTrue(os.path.isdir(self.job.backup))

    def test_unit_comparison_ignores_volatile_execution_metadata(self):
        def output(pid, status):
            return 'ExecStart={ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 -S -m biomerte ; ignore_errors=no ; pid=%d ; status=%d }\n' % (pid, status)
        with patch.object(self.job, 'command', side_effect=[output(42, 0), output(0, 15)]):
            first = helper.Bootstrap.unit(self.job, helper.CONTROLLER)
            second = helper.Bootstrap.unit(self.job, helper.CONTROLLER)
        self.assertEqual(first['ExecStart'], second['ExecStart'])

    def test_process_commandline_splits_real_nul_bytes(self):
        expected = ['/usr/bin/python3', '-S', '-m', 'biomerte', '--home', '/srv/biome', 'serve']
        path = os.path.join(self.tmp.name, 'proc-cmdline')
        with open(path, 'wb') as stream:
            stream.write(b'\x00'.join(value.encode('utf-8') for value in expected) + b'\x00')
        self.assertEqual(helper.process_argv(path), expected)

    def test_all_interface_listener_uses_loopback_probe(self):
        self.job.config = {'listen': '0.0.0.0', 'port': 8740}
        with patch.object(helper.socket, 'create_connection', return_value=Mock()) as connect:
            with patch.object(helper, 'build_opener', side_effect=OSError()):
                value = helper.Bootstrap.probe(self.job)
        self.assertTrue(value['listening'])
        connect.assert_called_once_with(('127.0.0.1', 8740), timeout=2)

    def test_ambiguous_listener_error_refuses_instead_of_assuming_down(self):
        self.job.config = {'listen': '127.0.0.1', 'port': 8740}
        with patch.object(helper.socket, 'create_connection', side_effect=OSError(errno.EACCES, 'denied')):
            with self.assertRaises(helper.Refused):
                helper.Bootstrap.probe(self.job)

    def test_copy_is_bounded_and_checks_exact_size(self):
        class Bounded:
            def read(inner, count):
                self.assertEqual(count, 65536)
                return source.read(count)
        target = os.path.join(self.tmp.name, 'copied.sqlite')
        with open(self.snapshot, 'rb') as source:
            self.job.copy_snapshot(Bounded(), target)
        self.assertEqual(helper.file_digest(target), self.manifest['snapshot_sha256'])

    def test_invalid_sqlite_rejected_even_with_matching_checksum(self):
        with open(self.snapshot, 'wb') as stream:
            stream.write(b'not a database')
        self.manifest.update(snapshot_bytes=os.stat(self.snapshot).st_size,
                             snapshot_sha256=helper.file_digest(self.snapshot))
        with self.assertRaises(sqlite3.DatabaseError):
            self.job.run()
        self.assertNotIn('stop', self.job.calls)
        self.original_restored()
