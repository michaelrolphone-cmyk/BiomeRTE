"""Pending-result polling must be independent of acknowledged history size."""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from biomerte.worker import Worker


class Remote:
    site, realm, node = 'site', 'site', 'provider'
    def call(self, name, args):
        raise AssertionError('No outstanding receipt should make a network call')


class RecoveryIndex(unittest.TestCase):
    def test_million_acknowledged_actions_do_not_get_scanned_by_idle_poll(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'worker.sqlite')
            db = sqlite3.connect(path)
            db.executescript('CREATE TABLE actions(id TEXT PRIMARY KEY,signature TEXT,state TEXT,result TEXT,resource TEXT);'
                             'CREATE TABLE result_retention_eligible(id TEXT PRIMARY KEY,signature TEXT);'
                             'CREATE TABLE result_acknowledged(id TEXT PRIMARY KEY);')
            db.execute("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<1000000) "
                       "INSERT INTO actions SELECT printf('%07d',x),'signature','succeeded','{}','resource' FROM n")
            db.execute('INSERT INTO result_retention_eligible SELECT id,signature FROM actions')
            db.execute('INSERT INTO result_acknowledged SELECT id FROM actions')
            db.commit()
            db.close()
            worker = Worker(path, Remote(), [])
            try:
                self.assertEqual(1000000, worker.db.execute('SELECT COUNT(*) FROM actions').fetchone()[0])
                self.assertEqual(0, worker.db.execute('SELECT COUNT(*) FROM result_recovery_pending').fetchone()[0])
                queries = []
                worker.db.set_trace_callback(queries.append)
                ticks = []
                def guard():
                    ticks.append(1)
                    return int(len(ticks) > 100)
                worker.db.set_progress_handler(guard, 100)
                worker.recover_result_acknowledgements()
                worker.cleanup_results()
                worker.db.set_progress_handler(None, 0)
                worker.db.set_trace_callback(None)
                self.assertLessEqual(len(ticks), 100, 'Idle poll exceeded 10000 SQLite VM instructions')
                polling = next(q for q in queries if 'FROM result_recovery_pending p' in q)
                plan = [row[3] for row in worker.db.execute('EXPLAIN QUERY PLAN ' + polling)]
                self.assertIn('result_recovery_pending', plan[0])
                self.assertTrue(any('SEARCH a ' in row for row in plan), plan)
                # No per-connection backfill once projection version is installed.
                worker.db.close()
                worker = Worker(path, Remote(), [])
                self.assertEqual(0, worker.db.execute('SELECT COUNT(*) FROM result_recovery_pending').fetchone()[0])
            finally:
                worker.db.close()

    def test_projection_tracks_terminal_update_ack_and_rollback(self):
        with tempfile.TemporaryDirectory() as folder:
            worker = Worker(os.path.join(folder, 'worker.sqlite'), Remote(), [])
            try:
                worker.db.execute("INSERT INTO actions VALUES('new','sig','uncertain','{}','r')")
                worker.db.execute("INSERT INTO result_retention_eligible VALUES('new','sig')")
                self.assertEqual([], worker.db.execute('SELECT * FROM result_recovery_pending').fetchall())
                worker.db.execute("UPDATE actions SET state='succeeded' WHERE id='new'")
                self.assertEqual([('new', 'sig')], worker.db.execute('SELECT * FROM result_recovery_pending').fetchall())
                worker.db.execute('BEGIN IMMEDIATE')
                worker.db.execute("INSERT INTO result_acknowledged VALUES('new')")
                self.assertEqual([], worker.db.execute('SELECT * FROM result_recovery_pending').fetchall())
                worker.db.rollback()
                self.assertEqual([('new', 'sig')], worker.db.execute('SELECT * FROM result_recovery_pending').fetchall())
                worker.retire_result('new', 'sig', 'succeeded', {})
                self.assertEqual([], worker.db.execute('SELECT * FROM result_recovery_pending').fetchall())
                self.assertEqual([('new',)], worker.db.execute('SELECT * FROM result_acknowledged').fetchall())
            finally:
                worker.db.close()
