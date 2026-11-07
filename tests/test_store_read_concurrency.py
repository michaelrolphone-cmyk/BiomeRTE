"""WAL reader regression tests; all state and journals are disposable."""
import os
import sqlite3
import tempfile
import threading
import unittest
from biomerte.app import Application, initialize
from biomerte.store import Store, Fault
from tests.test_ha import Journal


class StoreReadConcurrency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name, 'test')
        self.store.execute(lambda tx: tx.put('items', 'one', {'value': 'old'}))
        # A contention failure is deterministic and fast, not a 12 second test.
        connect = self.store.connect
        def short_connect():
            db = connect()
            db.execute('PRAGMA busy_timeout=50')
            return db
        self.store.connect = short_connect

    def test_reads_complete_while_append_owns_writer_lock(self):
        journal = Journal()
        self.store.remote = journal
        entered, release = threading.Event(), threading.Event()
        original = journal.append
        errors = []
        def append(version, entry):
            entered.set()
            if not release.wait(4):
                raise RuntimeError('Reader did not complete before append release')
            return original(version, entry)
        journal.append = append
        def write():
            try:
                self.store.execute(lambda tx: tx.put('items', 'one', {'value': 'new'}))
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=write)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            for cached in (False, True):
                self.assertEqual(self.store.execute(lambda tx: tx.need('items', 'one'),
                                 cached=cached, read_only=True), {'value': 'old'})
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.store.execute(lambda tx: tx.need('items', 'one'), read_only=True),
                         {'value': 'new'})

    def test_read_snapshot_is_consistent_across_concurrent_commit(self):
        def read(tx):
            first = tx.need('items', 'one')
            self.store.execute(lambda other: other.put('items', 'one', {'value': 'new'}))
            self.assertEqual(tx.need('items', 'one'), first)
            return first
        self.assertEqual(self.store.execute(read, read_only=True), {'value': 'old'})

    def test_fresh_read_catches_up_and_checks_hash(self):
        journal = Journal()
        self.store.remote = journal
        entry = {'seq': 1, 'prev': '', 'changes': [['items', 'one', {'value': 'remote'}]]}
        journal.append('0', entry)
        self.assertEqual(self.store.execute(lambda tx: tx.need('items', 'one'), read_only=True),
                         {'value': 'remote'})
        journal.entries[0]['changes'][0][2] = {'value': 'tampered'}
        with self.assertRaises(Fault) as caught:
            self.store.execute(lambda tx: tx.rows('items'), read_only=True)
        self.assertEqual(caught.exception.status, 409)

    def test_read_only_and_cached_mutations_are_rejected(self):
        for options in ({'read_only': True}, {'cached': True}):
            for change in (lambda tx: tx.put('items', 'one', {'value': 'bad'}),
                           lambda tx: tx.drop('items', 'one')):
                with self.assertRaises(Fault) as caught:
                    self.store.execute(change, **options)
                self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.store.execute(lambda tx: tx.need('items', 'one'), read_only=True),
                         {'value': 'old'})

    def test_application_read_metadata_does_not_require_writer_lock(self):
        home = os.path.join(self.tmp.name, 'app')
        initialize(home)
        app = Application(home)
        for realm in ('local', 'site'):
            db = app.stores[realm].connect()
            try:
                db.execute('BEGIN IMMEDIATE')
                self.assertTrue(app.call('bus.discover', realm=realm))
                self.assertEqual(app.call('work.list', realm=realm), [])
            finally:
                db.rollback()
                db.close()

    def test_fresh_read_rejects_discontinuity_without_committing(self):
        journal = Journal()
        self.store.remote = journal
        journal.append('0', {'seq': 1, 'prev': 'wrong', 'changes': []})
        with self.assertRaises(Fault) as caught:
            self.store.execute(lambda tx: tx.rows('items'), read_only=True)
        self.assertEqual(caught.exception.status, 503)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT seq,hash FROM position').fetchone(), (0, ''))

    def test_read_only_mutation_after_catchup_is_rejected(self):
        journal = Journal()
        self.store.remote = journal
        journal.append('0', {'seq': 1, 'prev': '',
                            'changes': [['items', 'one', {'value': 'remote'}]]})
        with self.assertRaises(Fault):
            self.store.execute(lambda tx: tx.put('items', 'one', {'value': 'bad'}), read_only=True)
        self.assertEqual(len(journal.entries), 1)
        self.assertEqual(self.store.execute(lambda tx: tx.need('items', 'one'), cached=True),
                         {'value': 'remote'})

    def test_fresh_read_retries_if_mirror_advances_after_head_fetch(self):
        journal = Journal()
        self.store.remote = journal
        original = journal.read
        advanced = []
        def read(name):
            result = original(name)
            if name == 'head' and not advanced:
                advanced.append(True)
                self.store.execute(lambda tx: tx.put('items', 'one', {'value': 'new'}))
            return result
        journal.read = read
        self.assertEqual(self.store.execute(lambda tx: tx.need('items', 'one'), read_only=True),
                         {'value': 'new'})


    def test_raw_sql_write_is_rejected_in_current_and_cached_snapshots(self):
        for options in ({'read_only': True}, {'cached': True}):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.execute(lambda tx: tx.db.execute('DELETE FROM records'), **options)
            self.assertEqual(self.store.execute(lambda tx: tx.need('items', 'one'), cached=True),
                             {'value': 'old'})

    def test_raw_sql_write_after_catchup_is_rejected(self):
        journal = Journal()
        self.store.remote = journal
        journal.append('0', {'seq': 1, 'prev': '',
                            'changes': [['items', 'one', {'value': 'remote'}]]})
        with self.assertRaises(sqlite3.OperationalError):
            self.store.execute(lambda tx: tx.db.execute('DELETE FROM records'), read_only=True)
        self.assertEqual(len(journal.entries), 1)
        self.assertEqual(self.store.execute(lambda tx: tx.need('items', 'one'), cached=True),
                         {'value': 'remote'})
        db = self.store.connect()
        try:
            self.assertEqual(db.execute('SELECT seq FROM position').fetchone()[0], 1)
        finally:
            db.close()
        self.assertEqual(self.store.execute(lambda tx: tx.need('items', 'one'), read_only=True),
                         {'value': 'remote'})

    def test_catchup_releases_writer_before_read_callback(self):
        journal = Journal()
        self.store.remote = journal
        journal.append('0', {'seq': 1, 'prev': '',
                            'changes': [['items', 'one', {'value': 'remote'}]]})
        def read(tx):
            first = tx.need('items', 'one')
            # Simulate other work while a status callback waits on its network.
            self.store.execute(lambda other: other.put('items', 'one', {'value': 'new'}))
            self.assertEqual(tx.need('items', 'one'), first)
            return first
        self.assertEqual(self.store.execute(read, read_only=True), {'value': 'remote'})
        self.assertEqual(self.store.execute(lambda tx: tx.need('items', 'one'), cached=True),
                         {'value': 'new'})
