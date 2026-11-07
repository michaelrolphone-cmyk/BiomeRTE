"""Timeout diagnostics never change transactions or disclose record payloads."""
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from biomerte.store import Store
from tests.test_ha import Journal


class StoreLockDiagnostics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name, 'test')
        connect = self.store.connect
        def short_connect():
            db = connect()
            db.execute('PRAGMA busy_timeout=50')
            return db
        self.store.connect = short_connect

    def test_timeout_identifies_callback_owner_without_payloads(self):
        entered, release = threading.Event(), threading.Event()
        errors = []
        def blocking_callback(tx):
            tx.put('test', 'secret-id', {'secret': 'NEVER_LOG_THIS_PAYLOAD'})
            entered.set()
            if not release.wait(3):
                raise RuntimeError('Diagnostic test timed out')
        def run():
            try:
                self.store.execute(blocking_callback)
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            with patch('biomerte.store.log.warning') as warning:
                with self.assertRaises(sqlite3.OperationalError):
                    self.store.execute(lambda tx: None)
                args = warning.call_args[0]
                rendered = args[0] % args[1:]
                self.assertIn('phase=callback', rendered)
                self.assertIn('blocking_callback', rendered)
                self.assertNotIn('NEVER_LOG_THIS_PAYLOAD', rendered)
                self.assertNotIn('secret-id', rendered)
        finally:
            release.set()
            worker.join(4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertIsNone(self.store._writer_diagnostic)
        self.assertEqual(self.store.execute(lambda tx: tx.need('test', 'secret-id'), read_only=True),
                         {'secret': 'NEVER_LOG_THIS_PAYLOAD'})

    def test_writer_tracking_ends_before_append_and_read_callback(self):
        journal = Journal()
        self.store.remote = journal
        append = journal.append
        def checked_append(version, entry):
            self.assertIsNone(self.store._writer_diagnostic)
            return append(version, entry)
        journal.append = checked_append
        self.store.execute(lambda tx: tx.put('test', 'one', 1))
        journal.append('1', {'seq': 2, 'prev': journal.read('head')[0]['hash'],
                             'changes': [['test', 'one', 2]]})
        def read(tx):
            self.assertIsNone(self.store._writer_diagnostic)
            return tx.need('test', 'one')
        self.assertEqual(self.store.execute(read, read_only=True), 2)
        self.assertIsNone(self.store._writer_diagnostic)

    def test_callback_failure_clears_tracking_and_rolls_back(self):
        def failure(tx):
            tx.put('test', 'one', 1)
            raise ValueError('test')
        with self.assertRaises(ValueError):
            self.store.execute(failure)
        self.assertIsNone(self.store._writer_diagnostic)
        self.assertIsNone(self.store.execute(lambda tx: tx.get('test', 'one'), read_only=True))

    def test_untracked_owner_and_logging_failure_preserve_sqlite_error(self):
        db = self.store.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            with patch('biomerte.store.log.warning', side_effect=RuntimeError('logger failed')):
                with self.assertRaises(sqlite3.OperationalError):
                    self.store.execute(lambda tx: None)
        finally:
            db.rollback()
            db.close()

    def test_conflict_retry_and_remote_exception_leave_no_owner(self):
        from biomerte.gateway_journal import JournalConflict
        from biomerte.store import Fault
        journal = Journal()
        self.store.remote = journal
        append = journal.append
        attempts = []
        def conflict_once(version, entry):
            self.assertIsNone(self.store._writer_diagnostic)
            attempts.append(version)
            if len(attempts) == 1:
                raise JournalConflict('Simulated competing commit', 409)
            return append(version, entry)
        journal.append = conflict_once
        self.store.execute(lambda tx: tx.put('test', 'one', 1))
        self.assertEqual(len(attempts), 2)
        self.assertIsNone(self.store._writer_diagnostic)
        def unavailable(version, entry):
            self.assertIsNone(self.store._writer_diagnostic)
            raise Fault('Simulated network failure', 503)
        journal.append = unavailable
        with self.assertRaises(Fault):
            self.store.execute(lambda tx: tx.put('test', 'one', 2))
        self.assertIsNone(self.store._writer_diagnostic)
        self.assertEqual(self.store.execute(lambda tx: tx.need('test', 'one'), cached=True), 1)

    def test_previous_owner_cannot_clear_successor_tracking(self):
        old = [threading.get_ident(), 'commit', 1]
        successor = [threading.get_ident(), 'replay', 2]
        self.store._writer_diagnostic = successor
        self.store._clear_writer_diagnostic(old)
        self.assertIs(self.store._writer_diagnostic, successor)
        self.store._clear_writer_diagnostic(successor)
        self.assertIsNone(self.store._writer_diagnostic)


    def test_owner_change_during_stack_collection_omits_stale_attribution(self):
        import sys
        frames = sys._current_frames
        for replacement in (None, [threading.get_ident(), 'successor', 2]):
            self.store._writer_diagnostic = [threading.get_ident(), 'stale-phase', 1]
            def changed_frames():
                with self.store._writer_diagnostic_lock:
                    self.store._writer_diagnostic = replacement
                return frames()
            with patch('biomerte.store.sys._current_frames', side_effect=changed_frames):
                with patch('biomerte.store.log.warning') as warning:
                    self.store._report_writer_timeout()
                    args = warning.call_args[0]
                    rendered = args[0] % args[1:]
                    self.assertIn('owner_changed_during_snapshot', rendered)
                    self.assertNotIn('stale-phase', rendered)
                    self.assertNotIn('stack=', rendered)
            self.assertIs(self.store._writer_diagnostic, replacement)
        self.store._writer_diagnostic = None
