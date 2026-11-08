"""Own-append confirmation uses a verified ACK, never a new remote read."""
import contextlib
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from biomerte.app import Application, initialize
from biomerte.identity import sign
from biomerte.store import Fault, Records, Store, digest
from tests.test_ha import Journal


class AcknowledgedAppend(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        initialize(self.tmp.name)
        self.app = Application(self.tmp.name)
        self.store = self.app.stores['site']
        self.journal = Journal()
        self.store.remote = self.journal

    def create(self, request='create', item='task', auth=None):
        return self.app.call('work.create', {'kind': 'task', 'title': 'Test', 'id': item},
                             request, auth=auth)

    def position(self):
        with contextlib.closing(self.store.connect()) as db:
            return db.execute('SELECT seq,hash FROM position WHERE id=1').fetchone()

    def rows(self, kind):
        return self.store.execute(lambda tx: tx.rows(kind), cached=True)

    def trace_remote(self):
        calls = []
        read, append = self.journal.read, self.journal.append
        def checked():
            self.assertIsNone(self.store._writer_diagnostic)
            with contextlib.closing(self.store.connect()) as db:
                db.execute('PRAGMA busy_timeout=0')
                db.execute('BEGIN IMMEDIATE')
                db.rollback()
        def counted_read(name):
            checked()
            calls.append('read:' + name)
            return read(name)
        def counted_append(version, entry):
            checked()
            calls.append('append')
            return append(version, entry)
        self.journal.read = counted_read
        self.journal.append = counted_append
        return calls

    def test_success_has_two_calls_and_atomically_persists_receipt(self):
        calls = self.trace_remote()
        result = self.create()
        self.assertEqual(calls, ['read:head', 'append'])
        head, unused = Journal.read(self.journal, 'head')
        self.assertEqual(self.position(), (head['seq'], head['hash']))
        self.assertEqual([row['id'] for row in self.rows('work')], ['task'])
        receipts = self.rows('_receipt')
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]['result'], result)
        # A new Store object sees all durable data without needing catch-up.
        self.app.stores['site'] = Store(self.tmp.name, 'site', self.journal)
        calls[:] = []
        self.assertEqual(self.create(), result)
        self.assertEqual(calls, ['read:head'])
        self.assertEqual(len(self.journal.entries), 1)

    def test_remote_io_never_holds_writer_during_missing_history_or_commit(self):
        self.journal.append('0', {'seq': 1, 'prev': '',
                                 'changes': [['test', 'remote', {'value': 1}]]})
        calls = self.trace_remote()
        self.create()
        self.assertEqual(calls, ['read:head', 'read:log/00000000000000000001', 'append'])
        self.assertEqual(self.rows('test'), [{'value': 1}])

    def test_delayed_ack_does_not_overwrite_already_applied_entry(self):
        append = self.journal.append
        def delayed(version, entry):
            head = append(version, entry)
            self.assertEqual([row['id'] for row in self.app.call('work.list')], ['task'])
            return head
        self.journal.append = delayed
        original_put = Records.put
        def checked_put(tx, kind, key, value):
            owner = self.store._writer_diagnostic
            self.assertFalse(owner and owner[1] == 'replay-acknowledged')
            return original_put(tx, kind, key, value)
        with patch.object(Records, 'put', checked_put):
            self.assertEqual(self.create()['id'], 'task')
        self.assertEqual(self.position()[0], 1)

    def test_delayed_ack_never_regresses_newer_local_commit_or_receipt(self):
        append = self.journal.append
        def delayed(version, entry):
            head = append(version, entry)
            if entry['seq'] == 1:
                self.store.execute(lambda tx: tx.put('work', 'task', {'id': 'task', 'title': 'Newer'}))
            return head
        self.journal.append = delayed
        self.assertEqual(self.create()['title'], 'Test')
        self.assertEqual(self.rows('work'), [{'id': 'task', 'title': 'Newer'}])
        self.assertEqual(self.position(), (2, digest(self.journal.entries[1])))
        self.assertEqual(self.rows('_receipt')[0]['result']['title'], 'Test')
        self.assertEqual(len(self.journal.entries), 2)

    def test_fresh_read_sees_later_remote_commit_after_own_ack(self):
        append = self.journal.append
        def delayed(version, entry):
            head = append(version, entry)
            append(str(entry['seq']), {'seq': entry['seq'] + 1, 'prev': head['hash'],
                                      'changes': [['test', 'later', 'newer']]})
            return head
        self.journal.append = delayed
        self.create()
        self.assertEqual(self.position()[0], 1)
        self.assertEqual(self.rows('test'), [])
        calls = self.trace_remote()
        self.assertEqual(self.store.execute(lambda tx: tx.rows('test'), read_only=True), ['newer'])
        self.assertEqual(calls, ['read:head', 'read:log/00000000000000000002'])
        self.assertEqual(self.position()[0], 2)

    def interrupted_local_replay(self, exception):
        original_put = Records.put
        replayed = []
        def interrupted(tx, kind, key, value):
            result = original_put(tx, kind, key, value)
            owner = self.store._writer_diagnostic
            if owner and owner[1] == 'replay-acknowledged':
                replayed.append(kind)
                if len(replayed) == 2:
                    raise exception
            return result
        with patch.object(Records, 'put', interrupted):
            with self.assertRaises(type(exception)):
                self.create()
        self.assertEqual(len(replayed), 2)
        self.assertEqual(self.position(), (0, ''))
        self.assertEqual(self.rows('_receipt'), [])
        self.assertEqual(self.rows('work'), [])
        self.assertIsNone(self.store._writer_diagnostic)
        self.assertEqual(len(self.journal.entries), 1)
        self.app.stores['site'] = Store(self.tmp.name, 'site', self.journal)
        self.assertEqual(self.create()['id'], 'task')
        self.assertEqual(len(self.journal.entries), 1)

    def test_failed_local_replay_is_atomic_and_retry_recovers_receipt(self):
        self.interrupted_local_replay(ValueError('local failure'))

    def test_interrupted_local_replay_is_atomic_and_retry_recovers_receipt(self):
        self.interrupted_local_replay(KeyboardInterrupt())

    def test_failed_local_commit_rolls_back_all_changes_and_retry_recovers(self):
        connect = self.store.connect
        state = {'fail': True}
        store = self.store
        class Connection:
            def __init__(self):
                self.db = connect()
            def __getattr__(self, name):
                return getattr(self.db, name)
            def commit(self):
                owner = store._writer_diagnostic
                if state['fail'] and owner and owner[1] == 'commit':
                    state['fail'] = False
                    raise sqlite3.OperationalError('Simulated final commit failure')
                self.db.commit()
        self.store.connect = Connection
        with self.assertRaises(sqlite3.OperationalError):
            self.create()
        self.assertEqual(self.position(), (0, ''))
        self.assertEqual(self.rows('work'), [])
        self.assertEqual(self.rows('_receipt'), [])
        self.assertEqual(len(self.journal.entries), 1)
        self.assertEqual(self.create()['id'], 'task')
        self.assertEqual(len(self.journal.entries), 1)

    def test_matching_ack_applies_exact_encoded_entry_not_later_mutation(self):
        append = self.journal.append
        def changed(version, entry):
            head = append(version, entry)
            entry['changes'].append(['test', 'injected', True])
            return head
        self.journal.append = changed
        self.assertEqual(self.create()['id'], 'task')
        self.assertEqual(self.position(), (1, digest(self.journal.entries[0])))
        self.assertEqual(len(self.rows('_receipt')), 1)
        self.assertEqual(self.rows('test'), [])
        self.journal.append = append
        self.assertEqual(self.create()['id'], 'task')
        self.assertEqual(self.rows('test'), [])
        self.assertEqual(len(self.journal.entries), 1)

    def test_matching_sequence_with_wrong_local_hash_is_rejected(self):
        append = self.journal.append
        def corrupted(version, entry):
            head = append(version, entry)
            self.app.call('work.list')
            with contextlib.closing(self.store.connect()) as db:
                db.execute("UPDATE position SET hash='corrupted' WHERE id=1")
            return head
        self.journal.append = corrupted
        with self.assertRaises(Fault) as caught:
            self.create()
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.position(), (1, 'corrupted'))

    def test_wrong_parent_anchor_is_rejected_without_replay(self):
        append = self.journal.append
        def corrupted(version, entry):
            head = append(version, entry)
            with contextlib.closing(self.store.connect()) as db:
                db.execute("UPDATE position SET hash='corrupted' WHERE id=1")
            return head
        self.journal.append = corrupted
        with self.assertRaises(Fault) as caught:
            self.create()
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(self.rows('work'), [])
        self.assertEqual(self.rows('_receipt'), [])

    def test_mirror_rewind_cannot_skip_missing_entries(self):
        self.store.execute(lambda tx: tx.put('test', 'prior', 1))
        append = self.journal.append
        def rewind(version, entry):
            head = append(version, entry)
            with contextlib.closing(self.store.connect()) as db:
                db.execute("UPDATE position SET seq=0,hash='' WHERE id=1")
            return head
        self.journal.append = rewind
        with self.assertRaises(Fault) as caught:
            self.create()
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(self.rows('work'), [])
        self.assertEqual(self.rows('_receipt'), [])

    def peer_auth(self):
        home = os.path.join(self.tmp.name, 'peer')
        cfg = initialize(home)
        peer = self.app.call('peer.trust', {'public_key': cfg['public_key'],
                             'label': 'Test', 'scopes': ['work.create']}, 'trust')
        envelope = {'site': self.app.site, 'realm': 'site', 'operation': 'work.create',
                    'arguments': {'kind': 'task', 'title': 'Test', 'id': 'task'}, 'id': 'create'}
        auth = {'actor': peer['id'], 'signature': sign(os.path.join(home, 'node.key'), envelope)}
        return peer, auth

    def test_cas_conflict_refreshes_revocation_and_rechecks_authorization(self):
        peer, auth = self.peer_auth()
        append = self.journal.append
        attempted = []
        def competing(version, entry):
            attempted.append(entry)
            revoked = dict(peer, enabled=False)
            append(version, {'seq': entry['seq'], 'prev': entry['prev'],
                             'changes': [['peers', peer['id'], revoked]]})
            return append(version, entry)
        self.journal.append = competing
        with self.assertRaises(Fault) as caught:
            self.create(auth=auth)
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(str(caught.exception), 'Revoked credential')
        self.assertEqual(len(attempted), 1)
        self.assertEqual(len(self.journal.entries), 2)
        self.assertEqual(self.rows('work'), [])

    def test_durable_receipt_retry_still_checks_latest_peer_revocation(self):
        peer, auth = self.peer_auth()
        result = self.create(auth=auth)
        self.assertEqual(self.create(auth=auth), result)
        head, version = self.journal.read('head')
        self.journal.append(version, {'seq': head['seq'] + 1, 'prev': head['hash'],
                            'changes': [['peers', peer['id'], dict(peer, enabled=False)]]})
        with self.assertRaises(Fault) as caught:
            self.create(auth=auth)
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(str(caught.exception), 'Revoked credential')
        self.assertEqual(len(self.journal.entries), 3)
        self.assertEqual(len(self.rows('work')), 1)

    def test_no_change_callback_still_contacts_fresh_head(self):
        calls = self.trace_remote()
        self.assertIsNone(self.store.execute(lambda tx: tx.get('test', 'missing')))
        self.assertEqual(calls, ['read:head'])
        self.assertEqual(self.journal.entries, [])
