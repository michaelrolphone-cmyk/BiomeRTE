import json
import sqlite3
import unittest
from biomerte.retention_index import initialize, advance, protected_by_roots, reset
from biomerte.store import Records, encode


class ReferenceIndexTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE records(kind TEXT,id TEXT,value TEXT,PRIMARY KEY(kind,id))')
        # Legacy data precedes projection/triggers.
        self.db.execute('INSERT INTO records VALUES(?,?,?)', ('unknown', 'legacy', encode({'ref': 'action:a'})))
        initialize(self.db)
        self.tx = Records(self.db)
        self.pool = {('actions', 'a'), ('artifacts', 'b')}
        self.records = {('actions', 'a'): {'id': 'a', 'ref': 'b'}, ('artifacts', 'b'): {'id': 'b'}}

    def ready(self, **kwargs):
        for unused in range(10000):
            if advance(self.db, **kwargs):
                return
        self.fail('index did not finish')

    def test_legacy_backfill_and_transitive_roots(self):
        self.assertFalse(advance(self.db, max_rows=1, max_chars=3))
        self.ready(max_rows=2, max_chars=3)
        self.assertEqual(protected_by_roots(self.tx, self.pool, self.records), self.pool)

    def test_changed_partial_record_restarts_and_deletion_unpins(self):
        advance(self.db, max_rows=1, max_chars=5)
        self.db.execute('UPDATE records SET value=?', (encode({'ref': 'b'}),))
        self.assertFalse(advance(self.db, max_rows=1, max_chars=5))
        self.ready(max_chars=5)
        self.assertEqual(protected_by_roots(self.tx, self.pool, self.records), {('artifacts', 'b')})
        self.db.execute('DELETE FROM records')
        self.ready()
        self.assertEqual(protected_by_roots(self.tx, self.pool, self.records), set())

    def test_oversized_strings_unicode_escapes_and_map_keys(self):
        value = {'padding': 'x' * 200000, '\\"☃': 'unused', 'action:a': 'yes'}
        self.db.execute('UPDATE records SET value=?', (encode(value),))
        self.assertFalse(advance(self.db, max_rows=64, max_chars=1000))
        offset = self.db.execute('SELECT offset FROM retention_ref_progress').fetchone()[0]
        self.assertLessEqual(offset, 1000)
        self.ready(max_chars=997)
        self.assertEqual(protected_by_roots(self.tx, self.pool, self.records), self.pool)
        from biomerte.retention_index import reference_hash
        refs = {r[0] for r in self.db.execute('SELECT hash FROM retention_ref_tokens')}
        self.assertIn(reference_hash('\\"☃'), refs)
        self.assertNotIn(reference_hash('x' * 200000), refs)

    def test_non_bmp_escaped_reference_at_identifier_limit(self):
        alias = '\U0001f642' * 512
        self.records[('actions', 'a')]['id'] = alias
        self.db.execute('UPDATE records SET value=?', (encode({'ref': 'action:' + alias}),))
        self.ready(max_chars=97)
        self.assertEqual(protected_by_roots(self.tx, self.pool, self.records), self.pool)

    def test_restore_and_reset_rebuild(self):
        self.ready()
        self.db.execute('DELETE FROM records')
        self.db.execute('INSERT INTO records VALUES(?,?,?)', ('durable', 'new', encode({'ref': 'b'})))
        self.assertFalse(advance(self.db, max_rows=1, max_chars=1))
        self.ready()
        self.assertEqual(protected_by_roots(self.tx, self.pool, self.records), {('artifacts', 'b')})
        reset(self.db)
        self.assertFalse(advance(self.db, max_rows=1, max_chars=1))
        self.ready()
        self.assertEqual(protected_by_roots(self.tx, self.pool, self.records), {('artifacts', 'b')})

    def test_projection_and_dirty_queue_rollback_together(self):
        self.ready()
        self.db.commit()
        self.db.execute('UPDATE records SET value=?', (encode({'ref': 'b'}),))
        self.ready()
        self.db.rollback()
        self.assertEqual(protected_by_roots(self.tx, self.pool, self.records), self.pool)

    def test_alias_larger_than_index_limit_fails_closed(self):
        self.ready()
        self.records[('actions', 'a')]['id'] = 'z' * 513
        self.db.execute('DELETE FROM records')
        self.ready()
        self.assertEqual(protected_by_roots(self.tx, self.pool, self.records), self.pool)

    def test_key_changing_direct_update_invalidates_old_source(self):
        self.ready()
        self.db.execute('UPDATE records SET id=?,value=?', ('renamed', encode({'ref': 'b'})))
        self.ready()
        self.assertEqual(protected_by_roots(self.tx, self.pool, self.records), {('artifacts', 'b')})

    def test_marker_projection_upgrade_and_restart(self):
        self.db.execute('INSERT INTO records VALUES(?,?,?)', ('_retention', 'actions:a',
                        encode({'kind': 'actions', 'key': 'a', 'terminal_at': 7})))
        self.ready()
        self.assertEqual(self.db.execute('SELECT kind,key,terminal FROM retention_markers').fetchone(), ('actions', 'a', 7.0))
        self.db.execute('DROP TABLE retention_markers')
        initialize(self.db)
        self.assertEqual(self.db.execute('SELECT complete FROM retention_ref_cursor').fetchone()[0], 0)
        self.ready()
        self.assertEqual(self.db.execute('SELECT kind,key,terminal FROM retention_markers').fetchone(), ('actions', 'a', 7.0))
        initialize(self.db)
        self.assertEqual(self.db.execute('SELECT complete FROM retention_ref_cursor').fetchone()[0], 1)

    def test_hash_collisions_only_add_protection(self):
        from unittest.mock import patch
        with patch('biomerte.retention_index.reference_hash', lambda value: b'collision'):
            self.db.execute('UPDATE records SET value=?', (encode({'unrelated': 'value'}),))
            self.ready()
            self.assertEqual(protected_by_roots(self.tx, self.pool, self.records), self.pool)

    def test_token_dictionary_reclaims_unused_hashes(self):
        self.ready()
        self.db.execute('INSERT INTO records VALUES(?,?,?)', ('unknown', 'other', encode({'ref': 'action:a'})))
        self.ready()
        before = self.db.execute('SELECT count(*) FROM retention_ref_tokens').fetchone()[0]
        self.db.execute("DELETE FROM records WHERE id='legacy'")
        self.ready()
        self.assertEqual(self.db.execute('SELECT count(*) FROM retention_ref_tokens').fetchone()[0], before)
        self.assertEqual(protected_by_roots(self.tx, self.pool, self.records), self.pool)
        self.db.execute('DELETE FROM records')
        self.ready()
        self.assertEqual(self.db.execute('SELECT count(*) FROM retention_ref_tokens').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM retention_refs').fetchone()[0], 0)

    def test_rebuild_migrates_legacy_text_projection(self):
        self.ready()
        self.db.execute('DROP TABLE retention_refs')
        self.db.execute('DROP TABLE retention_ref_sources')
        self.db.execute('DROP TABLE retention_ref_tokens')
        self.db.execute('CREATE TABLE retention_refs(kind TEXT,id TEXT,ref TEXT,PRIMARY KEY(kind,id,ref))')
        self.db.execute('CREATE TABLE retention_ref_sources(kind TEXT,id TEXT,ignored INTEGER,submission TEXT,PRIMARY KEY(kind,id))')
        self.db.execute('UPDATE retention_ref_version SET version=2')
        initialize(self.db)
        self.assertEqual(self.db.execute('SELECT complete FROM retention_ref_cursor').fetchone()[0], 0)
        self.ready(max_chars=7)
        self.assertEqual(protected_by_roots(self.tx, self.pool, self.records), self.pool)
        self.db.execute('DROP TABLE retention_ref_version')
        initialize(self.db)
        self.ready(max_chars=7)
        self.assertEqual(protected_by_roots(self.tx, self.pool, self.records), self.pool)

    def test_exempt_settled_sources(self):
        self.db.execute('DELETE FROM records')
        for kind, key, value in [('leases', 'l', {'state': 'released', 'task': 'a'}),
                                 ('actions', 'old', {'result_expired': True, 'ref': 'b'}),
                                 ('_meta', 'x', {'ref': 'a'}),
                                 ('_retention', 'x', {'ref': 'b'})]:
            self.db.execute('INSERT INTO records VALUES(?,?,?)', (kind, key, encode(value)))
        self.ready()
        self.assertEqual(protected_by_roots(self.tx, self.pool, self.records), set())


class ApplicationIndexTests(unittest.TestCase):
    def test_each_tick_advances_when_sweep_not_due_and_when_disabled(self):
        import tempfile
        from unittest.mock import patch
        from biomerte.app import Application, initialize as initialize_app
        from biomerte import retention_index
        with tempfile.TemporaryDirectory() as directory:
            initialize_app(directory)
            app = Application(directory)
            app.config['operational_retention'] = {'enabled': False, 'index_chars': 17, 'index_rows': 2}
            app.retention_next['site'] = float('inf')
            calls = []
            original = retention_index.advance
            def record(db, **kwargs):
                calls.append(kwargs)
                return original(db, **kwargs)
            with patch.object(retention_index, 'advance', record):
                app.tick('site')
                app.tick('site')
            self.assertEqual(calls, [{'max_chars': 17, 'max_rows': 2}] * 2)
            state = app.stores['site'].execute(lambda tx: retention_index.status(tx.db), read_only=True)
            self.assertIn('dirty_records', state)
            self.assertIn('legacy_keys_remaining', state)
            self.assertFalse(state['ready'])

    def test_index_only_work_has_no_canonical_changes(self):
        import tempfile
        from biomerte.store import Store
        from biomerte.retention_index import status
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory, 'test')
            store.execute(lambda tx: tx.put('unknown', 'r', {'refs': ['a']}))
            def run(tx):
                self.assertTrue(advance(tx.db))
                self.assertEqual(tx.changes, {})
                self.assertTrue(status(tx.db)['ready'])
            store.execute(run)
