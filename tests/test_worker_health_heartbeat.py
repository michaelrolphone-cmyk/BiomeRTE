"""Health telemetry must not fsync at every fast idle poll (Python 3.4+)."""
import json
import os
import stat
import tempfile
import unittest
from unittest import mock

from biomerte.store import Fault
from biomerte.worker import Worker, run_worker


class Remote:
    site = 'site'
    realm = 'site'
    node = 'worker'

    def __init__(self):
        self.polls = 0

    def call(self, name, arguments, rid=None):
        if name != 'provider.poll':
            raise AssertionError('Unexpected remote operation: ' + name)
        self.polls += 1
        return {'fences': [], 'actions': [], 'activations': []}


class StopLoop(BaseException):
    pass


class WorkerHealthHeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.remote = Remote()
        self.path = os.path.join(self.temporary.name, 'worker-health.json')
        self.worker = self.make_worker()
        self.wall = 1000.0
        self.mono = 100.0
        self.fsync = os.fsync
        self.wall_patch = mock.patch('biomerte.worker.time.time',
                                     side_effect=lambda: self.wall)
        self.mono_patch = mock.patch('biomerte.worker.time.monotonic',
                                     side_effect=lambda: self.mono)
        self.wall_patch.start()
        self.mono_patch.start()
        self.addCleanup(self.wall_patch.stop)
        self.addCleanup(self.mono_patch.stop)

    def make_worker(self):
        # Exercise the real publication method and filesystem without importing
        # a particular supervised-command implementation or changing a ledger.
        worker = Worker.__new__(Worker)
        worker.path = os.path.join(self.temporary.name, 'worker.sqlite')
        worker.remote = self.remote
        return worker

    def advance(self, seconds):
        self.wall = round(self.wall + seconds, 6)
        self.mono = round(self.mono + seconds, 6)

    def read(self):
        with open(self.path) as source:
            return json.load(source)

    def publish(self, errors=None, pending=0, debt=None):
        self.worker.write_health([] if errors is None else errors, pending, debt)

    def test_first_write_replaces_existing_snapshot_and_fsyncs_file_and_directory(self):
        with open(self.path, 'w') as out:
            json.dump({'state': 'ready', 'observed_at': 999.9,
                       'provider': 'other'}, out)
        calls = []

        def sync(fd):
            calls.append('directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file')
            return self.fsync(fd)

        with mock.patch('biomerte.worker.os.fsync', side_effect=sync):
            self.publish()
        self.assertEqual(calls, ['file', 'directory'])
        self.assertEqual(self.read(), {'observed_at': 1000.0, 'provider': 'worker',
                                      'state': 'ready', 'pending': 0, 'blocked': []})

    def test_fast_unchanged_polls_keep_memory_fresh_without_replacing_or_fsyncing(self):
        with mock.patch('biomerte.worker.os.fsync', side_effect=self.fsync) as sync:
            self.publish()
            first_inode = os.stat(self.path).st_ino
            for unused in range(49):
                self.advance(.1)
                self.publish()
            self.assertEqual(sync.call_count, 2)
        self.assertEqual(os.stat(self.path).st_ino, first_inode)
        self.assertEqual(self.read()['observed_at'], 1000.0)
        self.assertEqual(self.worker.last_health['observed_at'], 1004.9)

    def test_five_second_heartbeat_is_durable(self):
        with mock.patch('biomerte.worker.os.fsync', side_effect=self.fsync) as sync:
            self.publish()
            self.advance(4.999)
            self.publish()
            self.assertEqual(sync.call_count, 2)
            self.advance(.001)
            self.publish()
            self.assertEqual(sync.call_count, 4)
        self.assertEqual(self.read()['observed_at'], 1005.0)

    def test_nested_reused_errors_and_debt_publish_immediately(self):
        errors = [{'kind': 'action', 'id': 'held', 'details': {'reason': 'first'}}]
        debt = {'operations': [{'id': 'old', 'guard': 'slot-a'}], 'truncated': False}
        with mock.patch('biomerte.worker.os.fsync', side_effect=self.fsync) as sync:
            self.publish(errors, 1, debt)
            self.advance(.1)
            errors[0]['details']['reason'] = 'changed'
            self.publish(errors, 1, debt)
            self.assertEqual(self.read()['blocked'][0]['details']['reason'], 'changed')
            self.advance(.1)
            debt['operations'][0]['guard'] = 'slot-b'
            self.publish(errors, 1, debt)
            self.assertEqual(self.read()['historical_debt']['operations'][0]['guard'], 'slot-b')
            self.assertEqual(sync.call_count, 6)

    def test_pending_provider_error_clear_and_debt_membership_changes_publish_immediately(self):
        errors = [{'kind': 'fence', 'id': 'lease', 'reason': 'held'}]
        with mock.patch('biomerte.worker.os.fsync', side_effect=self.fsync) as sync:
            self.publish(errors, 1)
            self.advance(.1)
            self.publish(errors, 2)
            self.assertEqual(self.read()['pending'], 2)
            self.advance(.1)
            self.remote.node = 'new-worker'
            self.publish(errors, 2)
            self.assertEqual(self.read()['provider'], 'new-worker')
            self.advance(.1)
            self.publish([], 0)
            self.assertEqual(self.read()['state'], 'ready')
            self.assertEqual(self.read()['blocked'], [])
            self.advance(.1)
            self.publish([], 0, {})
            self.assertIn('historical_debt', self.read())
            self.advance(.1)
            self.publish([], 0)
            self.assertNotIn('historical_debt', self.read())
            self.assertEqual(sync.call_count, 12)

    def test_wall_clock_forward_jump_publishes_without_waiting_for_monotonic_deadline(self):
        self.publish()
        self.mono += .1
        self.wall += 30
        self.publish()
        self.assertEqual(self.read()['observed_at'], 1030.0)

    def test_wall_clock_reversal_does_not_leave_future_dated_snapshot(self):
        self.publish()
        self.mono += .1
        self.wall -= 30
        self.publish()
        self.assertEqual(self.read()['observed_at'], 970.0)

    def test_monotonic_reversal_forces_publication(self):
        self.publish()
        self.mono -= 1
        self.wall += .1
        self.publish()
        self.assertEqual(self.read()['observed_at'], 1000.1)

    def test_deleted_target_is_recreated_before_heartbeat(self):
        self.publish()
        os.unlink(self.path)
        self.advance(.1)
        self.publish()
        self.assertEqual(self.read()['observed_at'], 1000.1)

    def test_replaced_regular_target_is_not_mistaken_for_cached_publication(self):
        self.publish()
        original = self.read()
        replacement = self.path + '.external'
        with open(replacement, 'w') as out:
            json.dump(original, out)
        os.replace(replacement, self.path)
        self.advance(.1)
        with mock.patch('biomerte.worker.os.fsync', side_effect=self.fsync) as sync:
            self.publish()
        self.assertEqual(sync.call_count, 2)
        self.assertEqual(self.read()['observed_at'], 1000.1)

    def test_in_place_content_and_metadata_changes_invalidate_cache(self):
        self.publish()
        initial = os.stat(self.path)
        with open(self.path, 'r+') as out:
            body = out.read()
            out.seek(0)
            out.write(body.replace('"ready"', '"stale"'))
        os.utime(self.path, (initial.st_atime, initial.st_mtime + 1))
        self.assertEqual(os.stat(self.path).st_ino, initial.st_ino)
        self.advance(.1)
        self.publish()
        self.assertEqual(self.read()['state'], 'ready')
        self.assertEqual(self.read()['observed_at'], 1000.1)

    def test_unsafe_target_is_rejected_even_when_semantics_and_deadline_allow_skip(self):
        victim = self.path + '.victim'
        with open(victim, 'w') as out:
            out.write('preserve this file')
        for kind in ('symlink', 'hardlink', 'fifo'):
            with self.subTest(kind=kind):
                if os.path.lexists(self.path):
                    os.unlink(self.path)
                self.publish()
                os.unlink(self.path)
                if kind == 'symlink':
                    os.symlink(victim, self.path)
                elif kind == 'hardlink':
                    os.link(victim, self.path)
                else:
                    os.mkfifo(self.path)
                self.advance(.1)
                with self.assertRaises((Fault, OSError)):
                    self.publish()
                with open(victim) as source:
                    self.assertEqual(source.read(), 'preserve this file')
                os.unlink(self.path)

    def test_failed_file_fsync_does_not_suppress_retry(self):
        with mock.patch('biomerte.worker.os.fsync',
                        side_effect=OSError('injected file fsync failure')):
            with self.assertRaises(OSError):
                self.publish()
        self.assertIsNone(getattr(self.worker, '_health_publication', None))
        self.advance(.1)
        with mock.patch('biomerte.worker.os.fsync', side_effect=self.fsync) as sync:
            self.publish()
        self.assertEqual(sync.call_count, 2)
        self.assertEqual(self.read()['observed_at'], 1000.1)

    def test_failed_directory_fsync_retries_even_if_replacement_is_visible(self):
        def sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError('injected directory fsync failure')
            return self.fsync(fd)

        with mock.patch('biomerte.worker.os.fsync', side_effect=sync):
            with self.assertRaises(OSError):
                self.publish()
        self.assertEqual(self.read()['observed_at'], 1000.0)
        self.assertIsNone(getattr(self.worker, '_health_publication', None))
        self.advance(.1)
        with mock.patch('biomerte.worker.os.fsync', side_effect=self.fsync) as retry:
            self.publish()
        self.assertEqual(retry.call_count, 2)
        self.assertEqual(self.read()['observed_at'], 1000.1)

    def test_new_worker_never_borrows_an_old_instance_publication_cache(self):
        self.publish()
        self.worker = self.make_worker()
        self.advance(.1)
        with mock.patch('biomerte.worker.os.fsync', side_effect=self.fsync) as sync:
            self.publish()
        self.assertEqual(sync.call_count, 2)
        self.assertEqual(self.read()['observed_at'], 1000.1)

    def test_real_idle_run_loop_keeps_configured_tenth_second_polling(self):
        sleeps = []

        def next_cycle(seconds):
            sleeps.append(seconds)
            self.advance(seconds)
            if len(sleeps) == 61:
                raise StopLoop()

        with mock.patch('biomerte.worker.time.sleep', side_effect=next_cycle):
            with mock.patch('biomerte.worker.os.fsync', side_effect=self.fsync) as sync:
                with self.assertRaises(StopLoop):
                    run_worker(self.temporary.name, self.remote,
                               {'offers': [], 'parallelism': 4, 'poll_seconds': .1}, False)
        self.assertEqual(sleeps, [.1] * 61)
        self.assertEqual(self.remote.polls, 61)
        self.assertEqual(sync.call_count, 4)
        self.assertEqual(self.read()['observed_at'], 1005.0)


if __name__ == '__main__':
    unittest.main()
