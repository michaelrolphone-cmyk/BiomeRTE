"""Future health snapshots have one crash-safe, no-follow staging slot."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from biomerte.store import Fault
from biomerte.worker import Worker


class Remote:
    node = 'provider'


def health_writer(directory):
    worker = Worker.__new__(Worker)
    worker.path = os.path.join(directory, 'worker.sqlite')
    worker.remote = Remote()
    return worker


class WorkerHealthStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.worker = health_writer(self.tmp.name)
        self.path = os.path.join(self.tmp.name, 'worker-health.json')

    def read(self):
        with open(self.path) as stream:
            return json.load(stream)

    def test_repeated_failed_publish_reuses_single_stage_and_preserves_legacy(self):
        self.worker.write_health([], 1)
        legacy = self.path + '.legacy-uuid'
        with open(legacy, 'w') as stream:
            stream.write('prior crash history')
        for attempt in range(10):
            with patch('biomerte.worker.os.replace', side_effect=OSError('failed publish')):
                with self.assertRaises(OSError):
                    self.worker.write_health([], attempt + 2)
        self.assertEqual(1, self.read()['pending'])
        self.assertEqual(sorted(os.listdir(self.tmp.name)), sorted([
            'worker-health.json', 'worker-health.json.legacy-uuid',
            'worker-health.json.pending-v1', 'worker-health.json.write-v1.lock']))
        self.worker.write_health([], 99)
        self.assertEqual(99, self.read()['pending'])
        self.assertFalse(os.path.exists(self.path + '.pending-v1'))
        with open(legacy) as stream:
            self.assertEqual('prior crash history', stream.read())

    def test_real_crash_before_rename_releases_lock_and_next_write_reuses_stage(self):
        self.worker.write_health([], 1)
        code = '''import os,sys
from tests.test_worker_health_storage import health_writer
import biomerte.worker
biomerte.worker.os.replace=lambda *args: os._exit(73)
health_writer(sys.argv[1]).write_health([],2)
'''
        child = subprocess.Popen([sys.executable, '-c', code, self.tmp.name])
        self.assertEqual(73, child.wait(timeout=20))
        self.assertEqual(1, self.read()['pending'])
        self.assertTrue(os.path.isfile(self.path + '.pending-v1'))
        self.worker.write_health([], 3)
        self.assertEqual(3, self.read()['pending'])
        self.assertFalse(os.path.exists(self.path + '.pending-v1'))

    def test_symlink_hardlink_and_nonregular_paths_are_rejected_without_target_writes(self):
        target = os.path.join(self.tmp.name, 'unrelated-data')
        with open(target, 'w') as stream:
            stream.write('preserve unrelated bytes')
        for suffix in ('', '.pending-v1', '.write-v1.lock'):
            name = self.path + suffix
            for kind in ('symlink', 'hardlink', 'fifo', 'directory'):
                if os.path.lexists(name):
                    os.unlink(name)
                if kind == 'symlink':
                    os.symlink(target, name)
                elif kind == 'hardlink':
                    os.link(target, name)
                elif kind == 'fifo':
                    os.mkfifo(name)
                else:
                    os.mkdir(name)
                with self.assertRaises((OSError, Fault)):
                    self.worker.write_health([], 1)
                with open(target) as stream:
                    self.assertEqual('preserve unrelated bytes', stream.read())
                if kind == 'directory':
                    os.rmdir(name)
                else:
                    os.unlink(name)

    def test_concurrent_process_writers_publish_whole_snapshots(self):
        self.worker.write_health([], 0)
        code = '''import sys
from tests.test_worker_health_storage import health_writer
worker=health_writer(sys.argv[1])
for n in range(25):
    worker.write_health([{'writer':sys.argv[2], 'number':n, 'payload':'x'*4000}], n)
'''
        children = [subprocess.Popen([sys.executable, '-c', code, self.tmp.name, str(i)]) for i in range(4)]
        try:
            deadline = time.monotonic() + 20
            while any(child.poll() is None for child in children):
                value = self.read()
                self.assertEqual('provider', value['provider'])
                if value['blocked']:
                    self.assertEqual(value['pending'], value['blocked'][0]['number'])
                    self.assertEqual('x' * 4000, value['blocked'][0]['payload'])
                if time.monotonic() > deadline:
                    self.fail('Concurrent health writers stalled')
                time.sleep(.001)
            self.assertEqual([0] * 4, [child.returncode for child in children])
            self.assertFalse(os.path.exists(self.path + '.pending-v1'))
            self.assertEqual(['worker-health.json', 'worker-health.json.write-v1.lock'],
                             sorted(os.listdir(self.tmp.name)))
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait()
