import gc
import contextlib
import inspect
import sqlite3
import sys
import tempfile
import threading
import unittest
import weakref
from biomerte.store import Store, _IDLE_CONNECTIONS, _CONNECTIONS_LOCK


class ConnectionReuseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name, 'site')

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_reuse_keeps_full_durability_and_observes_external_commits(self):
        first = self.store.execute(lambda tx: (
            id(tx.db), tx.db.execute('PRAGMA synchronous').fetchone()[0]),
            read_only=True)
        with contextlib.closing(self.store.connect()) as other:
            other.execute("INSERT INTO records VALUES('work','external','42')")
        second = self.store.execute(lambda tx: (id(tx.db), tx.get('work', 'external')),
                                    read_only=True)
        self.assertEqual(first[0], second[0])
        self.assertEqual(first[1], 2)
        self.assertEqual(second[1], 42)
        self.store.execute(lambda tx: tx.put('work', 'next', 43))
        self.assertEqual(self.store.execute(lambda tx: tx.get('work', 'next'), read_only=True), 43)

    def test_failure_rolls_back_and_next_write_is_not_query_only(self):
        def fail(tx):
            tx.put('work', 'rolled-back', True)
            raise ValueError('injected failure')
        with self.assertRaises(ValueError):
            self.store.execute(fail)
        self.assertIsNone(self.store.execute(lambda tx: tx.get('work', 'rolled-back'), read_only=True))
        self.store.execute(lambda tx: tx.put('work', 'committed', True))
        self.assertTrue(self.store.execute(lambda tx: tx.get('work', 'committed'), read_only=True))

    def test_exclusive_checkout_does_not_serialize_independent_readers(self):
        entered = threading.Event()
        release = threading.Event()
        failures = []
        held = []
        def reader():
            try:
                def read(tx):
                    held.append(id(tx.db))
                    tx.db.execute('SELECT seq FROM position').fetchone()
                    entered.set()
                    release.wait(5)
                self.store.execute(read, read_only=True)
            except BaseException as exc:
                failures.append(exc)
        thread = threading.Thread(target=reader)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            second = self.store.execute(lambda tx: id(tx.db), read_only=True)
            self.assertNotEqual(held[0], second)
            self.store.execute(lambda tx: tx.put('work', 'concurrent', True))
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        # A connection first used by the other thread can safely be borrowed.
        self.assertTrue(self.store.execute(lambda tx: tx.get('work', 'concurrent'), read_only=True))

    def test_idle_bound_does_not_limit_active_checkouts(self):
        with contextlib.ExitStack() as stack:
            connections = [stack.enter_context(self.store.connection()) for unused in range(12)]
            self.assertEqual(len(set(id(db) for db in connections)), 12)
            for db in connections:
                self.assertEqual(db.execute('PRAGMA synchronous').fetchone()[0], 2)
        self.assertEqual(sum(owner is self.store._connection_key for owner, db in _IDLE_CONNECTIONS), 8)

    def test_closed_or_failed_connection_is_discarded(self):
        with self.store.connection() as db:
            db.close()
        self.assertFalse(any(owner is self.store._connection_key for owner, db in _IDLE_CONNECTIONS))
        self.assertEqual(self.store.execute(lambda tx: 42, read_only=True), 42)

    def test_store_disposal_closes_idle_handles_even_when_connection_is_referenced(self):
        other = Store(self.directory.name, 'disposable')
        with other.connection() as db:
            self.assertEqual(db.execute('SELECT 1').fetchone()[0], 1)
        del other
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute('SELECT 1')

    def test_many_live_stores_cannot_accumulate_unbounded_idle_handles(self):
        stores = []
        for index in range(20):
            store = Store(self.directory.name, 'many-' + str(index))
            stores.append(store)
            store.execute(lambda tx: 1, read_only=True)
        self.assertLessEqual(len(_IDLE_CONNECTIONS), 8)
        self.assertEqual(stores[0].execute(lambda tx: 42, read_only=True), 42)
        for store in stores:
            store.close()

    def test_cyclic_store_finalization_can_reenter_idle_pool_cleanup(self):
        other = Store(self.directory.name, 'cyclic')
        other.cycle = [other]
        with other.connection() as db:
            self.assertEqual(db.execute('SELECT 1').fetchone()[0], 1)
        del other
        # Allocation while managing idle handles may trigger cyclic collection.
        with _CONNECTIONS_LOCK:
            gc.collect()
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute('SELECT 1')

    def test_cyclic_gc_during_checkout_preserves_the_exact_warm_connection(self):
        gc.collect()
        enabled, previous_trace = gc.isenabled(), sys.gettrace()
        gc.disable()
        observed = []
        try:
            with self.store.connection() as warm:
                self.assertEqual(warm.execute('SELECT 1').fetchone()[0], 1)
            other = Store(self.directory.name, 'checkout-cycle')
            other.cycle = [other]
            with other.connection() as orphan_db:
                self.assertEqual(orphan_db.execute('SELECT 1').fetchone()[0], 1)
            orphan = weakref.ref(other)
            del other
            code = Store.connection.__wrapped__.__code__
            lines, first_line = inspect.getsourcelines(Store.connection)
            check_line = next(first_line + index for index, line in enumerate(lines)
                              if line.strip().startswith('if ') and
                              'self._connection_key' in line)
            def collect_during_checkout(frame, event, arg):
                if (frame.f_code is code and event == 'line' and
                        frame.f_lineno == check_line and not observed):
                    observed.append(len(_IDLE_CONNECTIONS))
                    gc.collect()
                    observed.append(len(_IDLE_CONNECTIONS))
                return collect_during_checkout
            sys.settrace(collect_during_checkout)
            try:
                with self.store.connection() as reused:
                    self.assertIs(reused, warm)
                    self.assertEqual(reused.execute('SELECT 1').fetchone()[0], 1)
            finally:
                sys.settrace(previous_trace)
            self.assertEqual(len(observed), 2)
            self.assertEqual(observed[0] - observed[1], 1)
            self.assertIsNone(orphan())
            with self.assertRaises(sqlite3.ProgrammingError):
                orphan_db.execute('SELECT 1')
            with self.store.connection() as reused:
                self.assertIs(reused, warm)
        finally:
            sys.settrace(previous_trace)
            if enabled:
                gc.enable()

    def test_cyclic_gc_during_close_does_not_resurrect_or_lose_idle_handles(self):
        from biomerte import store as store_module
        gc.collect()
        enabled, previous_trace = gc.isenabled(), sys.gettrace()
        gc.disable()
        observed = []
        try:
            other = Store(self.directory.name, 'close-cycle')
            other.cycle = [other]
            with self.store.connection() as first:
                with other.connection() as orphan_db:
                    with self.store.connection() as second:
                        self.assertIsNot(first, second)
            orphan = weakref.ref(other)
            del other
            code = store_module._close_connections.__code__
            lines, first_line = inspect.getsourcelines(store_module._close_connections)
            check_line = next(first_line + index for index, line in enumerate(lines)
                              if line.strip() == 'if entry[0] is key:')
            def collect_during_close(frame, event, arg):
                if (frame.f_code is code and event == 'line' and
                        frame.f_lineno == check_line and not observed):
                    observed.append(len(_IDLE_CONNECTIONS))
                    gc.collect()
                    observed.append(len(_IDLE_CONNECTIONS))
                return collect_during_close
            sys.settrace(collect_during_close)
            try:
                self.store.close()
            finally:
                sys.settrace(previous_trace)
            self.assertEqual(len(observed), 2)
            self.assertEqual(observed[0] - observed[1], 1)
            self.assertIsNone(orphan())
            for db in (first, second, orphan_db):
                with self.assertRaises(sqlite3.ProgrammingError):
                    db.execute('SELECT 1')
            self.assertFalse(any(owner is self.store._connection_key
                                 for owner, unused in _IDLE_CONNECTIONS))
        finally:
            sys.settrace(previous_trace)
            if enabled:
                gc.enable()

    def test_uncommitted_changes_cannot_escape_checkout(self):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("INSERT INTO records VALUES('work','pending','true')")
        self.assertIsNone(self.store.execute(lambda tx: tx.get('work', 'pending'), read_only=True))


if __name__ == '__main__':
    unittest.main()
