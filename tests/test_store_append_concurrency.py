"""Remote CAS never holds the SQLite writer lock; temporary state only."""
import tempfile
import threading
import unittest
from biomerte.app import Application, initialize
from biomerte.store import Fault, Store, Etcd, digest
from tests.test_ha import Journal
from biomerte.gateway_journal import GatewayJournal
from unittest.mock import patch


class StoreAppendConcurrency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        initialize(self.tmp.name)
        self.app = Application(self.tmp.name)
        self.store = self.app.stores['site']
        self.journal = Journal()
        self.store.remote = self.journal
        original = self.store.connect
        def connect():
            db = original()
            db.execute('PRAGMA busy_timeout=50')
            return db
        self.store.connect = connect

    def create(self, request='create', item='task'):
        return self.app.call('work.create', {'kind': 'task', 'title': 'Test', 'id': item}, request)

    def held_append(self, committed):
        entered, release = threading.Event(), threading.Event()
        original = self.journal.append
        errors, results = [], []
        def append(version, entry):
            head = original(version, entry) if committed else None
            entered.set()
            if not release.wait(4):
                raise RuntimeError('Reader failed to finish while append response was held')
            return head if committed else original(version, entry)
        self.journal.append = append
        def write():
            try:
                results.append(self.create())
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=write)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            # A fresh reader must acquire writer lock if remote commit is ahead.
            rows = self.app.call('work.list')
            self.assertEqual([row['id'] for row in rows], ['task'] if committed else [])
            # Directly prove the network waiter owns no SQLite writer lock.
            db = self.store.connect()
            try:
                db.execute('BEGIN IMMEDIATE')
                db.rollback()
            finally:
                db.close()
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(self.create(), results[0])
        self.assertEqual(len(self.journal.entries), 1)

    def test_fresh_read_catches_up_while_committed_response_is_held(self):
        self.held_append(True)

    def test_unconfirmed_proposal_is_invisible_while_append_waits(self):
        self.held_append(False)

    def concurrent_writers(self, same_request):
        # These tests race real CAS and subsequent local receipt/catch-up work.
        # Use the production busy timeout for legitimate local transactions;
        # the append barrier independently detects a retained network lock.
        self.store.connect = lambda: Store.connect(self.store)
        barrier = threading.Barrier(2, timeout=3)
        first_append = threading.Event()
        original = self.journal.append
        errors, results = [], []
        def append(version, entry):
            if version == '0':
                first_append.set()
                barrier.wait()
            return original(version, entry)
        self.journal.append = append
        def write(index):
            try:
                suffix = 'same' if same_request else str(index)
                results.append(self.create('request-' + suffix, 'task-' + suffix))
            except BaseException as exc:
                errors.append(exc)
        workers = [threading.Thread(target=write, args=(index,)) for index in range(2)]
        # Race remote CAS, not local staging/fsync against a 50 ms timeout.
        # The first writer must release SQLite before the second can stage.
        workers[0].start()
        entered = first_append.wait(3)
        if entered:
            workers[1].start()
        else:
            barrier.abort()
        for worker in workers:
            if worker.ident is not None:
                worker.join(6)
        self.assertTrue(entered, 'First writer did not reach remote append')
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len(self.journal.entries), 1 if same_request else 2)
        self.assertEqual(len(self.app.call('work.list')), 1 if same_request else 2)
        if same_request:
            self.assertEqual(results[0], results[1])

    def test_two_writers_re_evaluate_after_cas_conflict(self):
        self.concurrent_writers(False)

    def test_two_same_request_writers_share_authoritative_receipt(self):
        self.concurrent_writers(True)

    def lost_response(self, exception):
        original = self.journal.append
        def append(version, entry):
            original(version, entry)
            raise exception
        self.journal.append = append
        with self.assertRaises(type(exception)):
            self.create()
        self.assertEqual(self.app.call('work.list', cached=True), [])
        self.journal.append = original
        # Reopen the disposable mirror to model recovery after process loss.
        self.store = Store(self.tmp.name, 'site', self.journal)
        self.app.stores['site'] = self.store
        result = self.create()
        self.assertEqual(result['id'], 'task')
        self.assertEqual(len(self.journal.entries), 1)
        self.assertEqual(self.create(), result)
        self.assertEqual([row['id'] for row in self.app.call('work.list', cached=True)], ['task'])

    def test_committed_append_with_lost_response_recovers_same_receipt(self):
        self.lost_response(Fault('Simulated lost response', 503))

    def test_interrupt_after_remote_commit_recovers_same_receipt(self):
        self.lost_response(KeyboardInterrupt())

    def test_callback_failure_rolls_back_without_append(self):
        def failing(tx):
            tx.put('test', 'uncommitted', {'value': 1})
            raise ValueError('Simulated callback failure')
        with self.assertRaises(ValueError):
            self.store.execute(failing)
        self.assertEqual(self.journal.entries, [])
        self.assertEqual(self.store.execute(lambda tx: tx.rows('test'), cached=True), [])

    def test_rejected_append_never_commits_proposal(self):
        def append(version, entry):
            raise Fault('Journal unavailable', 503)
        self.journal.append = append
        with self.assertRaises(Fault):
            self.create()
        self.assertEqual(self.journal.entries, [])
        self.assertEqual(self.app.call('work.list', cached=True), [])

    def test_invalid_acknowledgement_does_not_apply_staged_changes(self):
        def append(version, entry):
            return {'seq': entry['seq'], 'hash': 'incorrect'}
        self.journal.append = append
        with self.assertRaises(Fault):
            self.create()
        self.assertEqual(self.app.call('work.list', cached=True), [])


    def test_post_append_refresh_failure_recovers_receipt_on_retry(self):
        original_append = self.journal.append
        original_read = self.journal.read
        def unavailable(name):
            raise Fault('Simulated refresh failure', 503)
        def append(version, entry):
            head = original_append(version, entry)
            self.journal.read = unavailable
            return head
        self.journal.append = append
        with self.assertRaises(Fault):
            self.create()
        self.assertEqual(self.app.call('work.list', cached=True), [])
        self.journal.read = original_read
        self.journal.append = original_append
        self.assertEqual(self.create()['id'], 'task')
        self.assertEqual(len(self.journal.entries), 1)


    def test_acknowledgement_allows_unrelated_metadata(self):
        original = self.journal.append
        def append(version, entry):
            head = original(version, entry)
            head['metadata'] = {'trace': 'example'}
            return head
        self.journal.append = append
        self.assertEqual(self.create()['id'], 'task')
        self.assertEqual(len(self.journal.entries), 1)

    def test_acknowledgement_rejects_invalid_sequence_hash_and_types(self):
        for head in (None, {}, {'seq': 2, 'hash': 'wrong'},
                     {'seq': True, 'hash': 'wrong'}, {'seq': '1', 'hash': 'wrong'},
                     {'seq': 1, 'hash': None}):
            self.journal.append = lambda version, entry: head
            with self.assertRaises(Fault) as caught:
                self.create()
            self.assertEqual(caught.exception.status, 503)
            self.assertEqual(self.app.call('work.list', cached=True), [])
        # Use a correct hash to isolate sequence/type rejection.
        for seq in (True, '1', 2):
            self.journal.append = lambda version, entry: {'seq': seq, 'hash': digest(entry)}
            with self.assertRaises(Fault):
                self.create()
        self.assertEqual(self.app.call('work.list', cached=True), [])

    def test_gateway_and_etcd_append_acknowledgement_shapes(self):
        entry = {'seq': 1, 'prev': '', 'changes': []}
        expected = {'seq': 1, 'hash': digest(entry)}
        gateway = GatewayJournal({'origin': 'https://example.invalid',
                                  'key_file': '/unused'}, 'site')
        with patch.object(gateway, 'request', return_value=dict(expected, metadata='allowed')):
            head = gateway.append('0', entry)
        self.assertEqual(head['seq'], 1)
        self.assertEqual(head['hash'], expected['hash'])
        self.assertEqual(head['metadata'], 'allowed')
        etcd = Etcd({'endpoints': ['https://example.invalid']}, 'site')
        with patch.object(etcd, 'rpc', return_value={'succeeded': True, 'header': {'revision': '7'}}):
            self.assertEqual(etcd.append('0', entry), expected)
