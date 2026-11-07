"""Derived scalar lookup consistency and history-independent tick reads."""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from biomerte import process, providers, store as storage
from biomerte.api import Context
from biomerte.store import Store, Records, encode
from biomerte.gateway_journal import JournalConflict
from tests.test_ha import Journal


class RecordLookup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name, 'test')

    def selected(self, kind='runs', field='state', values=('running',)):
        return self.store.execute(lambda tx: tx.matching(kind, field, values), read_only=True)

    def test_put_drop_order_and_savepoint_rollback(self):
        def writes(tx):
            for key in ('z', 'a', 'm'):
                tx.put('runs', key, {'id': key, 'state': 'running'})
            tx.db.execute('SAVEPOINT trial')
            tx.put('runs', 'a', {'id': 'a', 'state': 'completed'})
            tx.drop('runs', 'z')
            tx.db.execute('ROLLBACK TO trial')
            tx.db.execute('RELEASE trial')
            self.assertEqual([r['id'] for r in tx.matching('runs', 'state', ('running',))], ['a', 'm', 'z'])
        self.store.execute(writes)
        self.store.execute(lambda tx: tx.drop('runs', 'm'))
        self.assertEqual([r['id'] for r in self.selected()], ['a', 'z'])
        self.assertEqual(self.selected(values=()), [])
        with self.assertRaises(ValueError):
            self.selected(field='unindexed')

    def legacy(self):
        path = os.path.join(self.tmp.name, 'legacy.sqlite')
        db = sqlite3.connect(path)
        db.executescript('CREATE TABLE records(kind TEXT,id TEXT,value TEXT,PRIMARY KEY(kind,id));'
                         'CREATE TABLE position(id INTEGER PRIMARY KEY,seq INTEGER,hash TEXT);'
                         "INSERT INTO position VALUES(1,7,'unchanged-anchor');")
        for kind, value in (('runs', {'id': 'r', 'state': 'running'}),
                            ('leases', {'id': 'l', 'state': 'bound'}),
                            ('actions', {'id': 'a', 'lease': 'l'})):
            db.execute('INSERT INTO records VALUES(?,?,?)', (kind, value['id'], encode(value)))
        for index in range(500):
            db.execute('INSERT INTO records VALUES(?,?,?)', ('events', str(index), encode({'body': 'x' * 2000})))
        db.commit()
        original = list(db.execute('SELECT * FROM records ORDER BY kind,id'))
        db.close()
        return path, original

    def test_initial_backfill_decodes_only_three_target_rows_and_reopen_zero(self):
        path, original = self.legacy()
        loads = storage.json.loads
        with patch.object(storage.json, 'loads', wraps=loads) as decode:
            upgraded = Store(self.tmp.name, 'legacy')
            self.assertEqual(decode.call_count, 3)
        with patch.object(storage.json, 'loads', wraps=loads) as decode:
            Store(self.tmp.name, 'legacy')
            self.assertEqual(decode.call_count, 0)
        db = upgraded.connect()
        try:
            self.assertEqual(list(db.execute('SELECT * FROM records ORDER BY kind,id')), original)
            self.assertEqual(db.execute('SELECT seq,hash FROM position').fetchone(), (7, 'unchanged-anchor'))
        finally:
            db.close()

    def test_interrupted_backfill_retries_without_changing_authority(self):
        path, original = self.legacy()
        index = Records._index
        visited = []
        def interrupted(tx, kind, key, value):
            index(tx, kind, key, value)
            visited.append(key)
            if len(visited) == 2:
                raise RuntimeError('Simulated migration interruption')
        with patch.object(Records, '_index', interrupted):
            with self.assertRaises(RuntimeError):
                Store(self.tmp.name, 'legacy')
        db = sqlite3.connect(path)
        try:
            self.assertEqual(list(db.execute('SELECT * FROM records ORDER BY kind,id')), original)
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'record_lookup%'").fetchone()[0], 0)
        finally:
            db.close()
        upgraded = Store(self.tmp.name, 'legacy')
        db = upgraded.connect()
        try:
            self.assertEqual(list(db.execute('SELECT * FROM records ORDER BY kind,id')), original)
            self.assertEqual(db.execute('SELECT count(*) FROM record_lookup').fetchone()[0], 3)
            self.assertEqual(db.execute('SELECT count(*) FROM record_lookup_dirty').fetchone()[0], 0)
        finally:
            db.close()

    def test_old_binary_sql_writes_are_correct_before_and_after_reupgrade(self):
        self.store.execute(lambda tx: tx.put('runs', 'old', {'id': 'old', 'state': 'running'}))
        db = self.store.connect()
        try:
            db.execute('UPDATE records SET value=? WHERE kind=? AND id=?',
                       (encode({'id': 'old', 'state': 'completed'}), 'runs', 'old'))
            db.execute('INSERT OR REPLACE INTO records VALUES(?,?,?)',
                       ('runs', 'new', encode({'id': 'new', 'state': 'running'})))
        finally:
            db.close()
        self.assertEqual([r['id'] for r in self.selected()], ['new'])
        self.store = Store(self.tmp.name, 'test')
        self.assertEqual([r['id'] for r in self.selected()], ['new'])
        db = self.store.connect()
        try:
            db.execute("DELETE FROM records WHERE kind='runs' AND id='new'")
        finally:
            db.close()
        self.assertEqual(self.selected(), [])
        self.store = Store(self.tmp.name, 'test')
        self.assertEqual(self.selected(), [])

    def test_remote_replay_drop_and_failed_cas_update_index_atomically(self):
        journal = Journal()
        self.store.remote = journal
        self.store.execute(lambda tx: tx.put('runs', 'r', {'id': 'r', 'state': 'running'}))
        mirror = Store(self.tmp.name, 'mirror', journal)
        self.assertEqual(mirror.execute(lambda tx: tx.matching('runs', 'state', ('running',)), read_only=True)[0]['id'], 'r')
        append = journal.append
        attempts = []
        def conflict_once(version, entry):
            # Proposed update must have rolled back both records and index.
            self.assertEqual(self.store.execute(lambda tx: tx.matching('runs', 'state', ('running',)), cached=True)[0]['id'], 'r')
            attempts.append(1)
            if len(attempts) == 1:
                raise JournalConflict('Simulated conflict', 409)
            return append(version, entry)
        journal.append = conflict_once
        self.store.execute(lambda tx: tx.put('runs', 'r', {'id': 'r', 'state': 'completed'}))
        self.assertEqual(len(attempts), 2)
        self.assertEqual(mirror.execute(lambda tx: tx.matching('runs', 'state', ('running',)), read_only=True), [])
        journal.append = append
        self.store.execute(lambda tx: tx.drop('runs', 'r'))
        self.assertEqual(mirror.execute(lambda tx: tx.matching('runs', 'state', ('completed',)), read_only=True), [])
        self.assertTrue(all(kind in ('runs',) for entry in journal.entries for kind, key, value in entry['changes']))

    def test_tick_and_recovery_decode_active_records_not_retained_history(self):
        def seed(tx, count):
            tx.put('_meta', 'events', 0)
            for index in range(count):
                key = 'history-%05d' % index
                tx.put('runs', key, {'id': key, 'state': 'completed', 'payload': 'x' * 4000})
                tx.put('leases', key, {'id': key, 'state': 'released', 'resource': 'port', 'task': key})
                tx.put('actions', key, {'id': key, 'lease': key, 'state': 'succeeded', 'payload': 'x' * 4000})
            tx.put('runs', 'live', {'id': 'live', 'state': 'running', 'name': 'test', 'rev': 'v', 'tokens': []})
            tx.put('process_versions', 'v', {'graph': {'nodes': {}}})
            tx.put('offers', 'offer', {'configuration': {'reservation_policy': {'release_after_result': True}}})
            tx.put('leases', 'live', {'id': 'live', 'state': 'bound', 'generation': 1, 'offer': 'offer', 'guard': 'g', 'resource': 'port', 'task': 'live'})
            tx.put('guards', 'g', {'holder': 'live', 'generation': 1})
            tx.put('actions', 'live', {'id': 'live', 'lease': 'live', 'generation': 1, 'state': 'succeeded'})
        counts = []
        for size in (10, 1000):
            self.store.execute(lambda tx: seed(tx, size))
            loads = storage.json.loads
            with patch.object(storage.json, 'loads', wraps=loads) as decode:
                self.assertEqual([r['id'] for r in self.store.execute(lambda tx: providers.active_on(tx, 'port'))], ['live'])
                self.store.execute(lambda tx: providers.release_task(tx, 'absent-task'))
                self.assertEqual(self.store.execute(lambda tx: process.tick(tx, Context(None))), 0)
                self.assertEqual(self.store.execute(lambda tx: providers.recover_reservations(tx, Context(None))), 1)
                counts.append(decode.call_count)
        self.assertEqual(counts[0], counts[1])
        self.assertLess(counts[1], 20)


    def test_lookup_vm_work_does_not_scan_settled_record_keys(self):
        def seed(tx):
            for index in range(2000):
                tx.put('runs', str(index), {'id': str(index), 'state': 'completed'})
            tx.put('runs', 'live', {'id': 'live', 'state': 'running'})
        self.store.execute(seed)
        def query(tx):
            steps = [0]
            def progress():
                steps[0] += 1
                return 0
            tx.db.set_progress_handler(progress, 1)
            try:
                rows = tx.matching('runs', 'state', ('running',))
            finally:
                tx.db.set_progress_handler(None, 0)
            self.assertEqual([row['id'] for row in rows], ['live'])
            self.assertLess(steps[0], 1000)
        self.store.execute(query, read_only=True)

    def test_old_sql_key_change_invalidates_both_sides(self):
        self.store.execute(lambda tx: tx.put('runs', 'old', {'id': 'old', 'state': 'running'}))
        db = self.store.connect()
        try:
            db.execute('UPDATE records SET id=?,value=? WHERE kind=? AND id=?',
                       ('new', encode({'id': 'new', 'state': 'running'}), 'runs', 'old'))
        finally:
            db.close()
        self.assertEqual([r['id'] for r in self.selected()], ['new'])
        self.store = Store(self.tmp.name, 'test')
        self.assertEqual([r['id'] for r in self.selected()], ['new'])


    def test_provider_poll_preserves_contents_fairness_without_history_decode(self):
        def seed(tx, count):
            for index in range(count):
                key = 'history-%05d' % index
                tx.put('leases', key, {'id': key, 'state': 'released', 'provider': 'device'})
                tx.put('actions', key, {'id': key, 'state': 'succeeded', 'provider': 'device',
                                       'lease': key, 'payload': 'x' * 4000})
            tx.put('leases', 'bound', {'id': 'bound', 'state': 'bound', 'provider': 'device'})
            tx.put('leases', 'fence', {'id': 'fence', 'state': 'fencing', 'provider': 'device'})
            tx.put('leases', 'other', {'id': 'other', 'state': 'fencing', 'provider': 'other'})
            for key, state, guard, lease, provider in (
                    ('a', 'queued', 'g1', 'bound', 'device'),
                    ('b', 'claimed', 'g1', 'bound', 'device'),
                    ('c', 'queued', 'g2', 'bound', 'device'),
                    ('d', 'queued', 'g3', 'fence', 'device'),
                    ('e', 'queued', 'g4', 'bound', 'other')):
                tx.put('actions', key, {'id': key, 'state': state, 'guard': guard,
                                       'resource': 'port', 'lease': lease, 'provider': provider})
            tx.put('activations', 'activation', {'id': 'activation', 'provider': 'device', 'state': 'pending'})
        counts = []
        for size in (10, 1000):
            self.store.execute(lambda tx: seed(tx, size))
            with patch.object(storage.json, 'loads', wraps=storage.json.loads) as decode:
                result = self.store.execute(lambda tx: providers.pending(tx, Context(None), 'device'), read_only=True)
                counts.append(decode.call_count)
            self.assertEqual([row['id'] for row in result['fences']], ['fence'])
            self.assertEqual([row['id'] for row in result['actions']], ['a', 'c', 'b'])
            self.assertEqual([row['id'] for row in result['activations']], ['activation'])
        self.assertEqual(counts[0], counts[1])
        self.assertLess(counts[1], 20)


    def test_dirty_and_clean_matching_use_same_typed_scalar_rules(self):
        def seed(tx):
            for key, value in (('null', None), ('boolean', True), ('integer', 1)):
                tx.put('runs', key, {'id': key, 'state': value})
            tx.put('runs', 'missing', {'id': 'missing'})
        self.store.execute(seed)
        for dirty in (False, True):
            if dirty:
                db = self.store.connect()
                try:
                    db.execute("UPDATE records SET value=value WHERE kind='runs'")
                finally:
                    db.close()
            self.assertEqual([r['id'] for r in self.selected(values=(None,))], ['null'])
            self.assertEqual([r['id'] for r in self.selected(values=(True,))], ['boolean'])
            self.assertEqual([r['id'] for r in self.selected(values=(1,))], ['integer'])
