"""Filesystem regression for the captured Pi gateway's SQLite connection churn.

All files and child processes belong to a disposable test directory. The real
SessionService idle-maintenance path is exercised without network requests.
"""
import contextlib
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest

from biomerte import store as store_module
from biomerte.session_runtime import SessionRuntime
from biomerte.session_service import SessionService
from biomerte.store import Store


@unittest.skipUnless(os.name == 'posix', 'SQLite Unix WAL sidefile lifecycle')
class GatewayStoreLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(self.directory.name, 'site')
        self.addCleanup(self.store.close)

    def sidefiles(self, store):
        return (store.path + '-wal', store.path + '-shm')

    def identity(self, path):
        observed = os.stat(path)
        return observed.st_dev, observed.st_ino

    def file_digest(self, path):
        with open(path, 'rb') as source:
            return hashlib.sha256(source.read()).hexdigest()

    def test_short_lived_read_reproduces_sidefile_creation_and_last_close_cleanup(self):
        # This is the old _execute connection lifetime, using the real SQLite
        # engine. It demonstrates why a SELECT can allocate a 32 KiB SHM file.
        wal, shm = self.sidefiles(self.store)
        baseline = self.file_digest(self.store.path)
        for unused in range(3):
            with contextlib.closing(self.store.connect()) as db:
                self.assertEqual(db.execute('SELECT seq FROM position').fetchone()[0], 0)
                self.assertTrue(os.path.isfile(wal))
                self.assertEqual(os.path.getsize(wal), 0)
                self.assertEqual(os.path.getsize(shm), 32768)
            self.assertFalse(os.path.exists(wal))
            self.assertFalse(os.path.exists(shm))
            self.assertEqual(self.file_digest(self.store.path), baseline)

    def test_real_idle_session_maintenance_retains_sidefiles_without_state_writes(self):
        calls = []
        def no_network(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError('Idle maintenance must not use the network')

        directory = os.path.join(self.directory.name, 'coordination')
        runtime = SessionRuntime(object(), directory, no_network, self.store)
        self.addCleanup(runtime.store.close)
        service = SessionService(self.directory.name, 'https://unused.invalid',
                                 None, None, no_network)
        service._runtime = runtime
        self.assertEqual(service(), {'active': False})
        warm = runtime.store.execute(lambda tx: tx.db, read_only=True)
        wal, shm = self.sidefiles(runtime.store)
        identities = [self.identity(path) for path in (wal, shm)]
        main_digest = self.file_digest(runtime.store.path)
        changes = warm.total_changes
        for unused in range(12):
            # Each call performs the actual _blocked/maintain/state reads.
            self.assertEqual(service(), {'active': False})
            self.assertIs(runtime.store.execute(lambda tx: tx.db, read_only=True), warm)
            self.assertEqual([self.identity(path) for path in (wal, shm)], identities)
        self.assertEqual(warm.total_changes, changes)
        self.assertEqual(self.file_digest(runtime.store.path), main_digest)
        self.assertEqual(os.path.getsize(wal), 0)
        self.assertEqual(os.path.getsize(shm), 32768)
        self.assertEqual(calls, [])
        runtime.store.close()
        self.assertFalse(os.path.exists(wal))
        self.assertFalse(os.path.exists(shm))

    def test_abrupt_process_exit_without_pool_cleanup_preserves_only_committed_state(self):
        # FULL durability must not depend on Store.close or a successful final
        # checkpoint. The child exits with a committed WAL and an open write tx.
        source = """import os
import sys
from biomerte.store import Store, Records
store = Store(sys.argv[1], 'crash')
store.execute(lambda tx: tx.put('work', 'committed', {'value': 42}))
with store.connection() as db:
    if db.execute('PRAGMA synchronous').fetchone()[0] != 2:
        os._exit(24)
    db.execute('BEGIN IMMEDIATE')
    Records(db).put('work', 'uncommitted', True)
    os._exit(23)
"""
        environment = dict(os.environ)
        package_root = os.path.dirname(os.path.dirname(os.path.abspath(store_module.__file__)))
        environment['PYTHONPATH'] = package_root
        environment['PYTHONDONTWRITEBYTECODE'] = '1'
        child = subprocess.Popen([sys.executable, '-c', source, self.directory.name],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env=environment)
        try:
            output, errors = child.communicate(timeout=20)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate()
        self.assertEqual(child.returncode, 23, (output, errors))
        path = os.path.join(self.directory.name, 'crash.sqlite')
        self.assertGreater(os.path.getsize(path + '-wal'), 0)
        self.assertTrue(os.path.isfile(path + '-shm'))
        restored = Store(self.directory.name, 'crash')
        self.addCleanup(restored.close)
        self.assertEqual(restored.execute(lambda tx: tx.get('work', 'committed'),
                                          read_only=True), {'value': 42})
        self.assertIsNone(restored.execute(lambda tx: tx.get('work', 'uncommitted'),
                                          read_only=True))
        restored.execute(lambda tx: tx.put('work', 'after-recovery', True))
        self.assertTrue(restored.execute(lambda tx: tx.get('work', 'after-recovery'),
                                         read_only=True))


if __name__ == '__main__':
    unittest.main()
