"""Bounded current-state receipts without weakening legacy idempotency."""
import os
import tempfile
import threading
import unittest
from unittest.mock import patch
from biomerte.api import Context, OPERATIONS, command, RECEIPT_RESULT_BYTES
from biomerte.store import Store, Fault, encode
from tests.test_ha import Journal


class ReceiptStreams(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name, 'test')
        self.ctx = Context(None)
        self.calls = 0
        def mutate(tx, ctx, value):
            self.calls += 1
            tx.put('current', 'value', value)
            return value
        self.ops = patch.dict(OPERATIONS, {'test.receipt': (mutate, {
            'value': {'type': 'any', 'required': True}}, True)})
        self.ops.start()
        self.addCleanup(self.ops.stop)

    def call(self, sequence, value=None, slot=0, ctx=None, store=None):
        request = {'stream': slot, 'sequence': sequence}
        return (store or self.store).execute(lambda tx: command(
            tx, ctx or self.ctx, 'test.receipt', {'value': value}, request))

    def fault(self, status, fn):
        with self.assertRaises(Fault) as caught:
            fn()
        self.assertEqual(status, caught.exception.status)

    def test_replay_conflict_retirement_restart(self):
        self.assertEqual({'x': 1}, self.call(1, {'x': 1}))
        self.assertEqual({'x': 1}, self.call(1, {'x': 1}))
        self.fault(409, lambda: self.call(1, {'x': 2}))
        self.fault(409, lambda: self.call(3))
        self.call(2, 'next')
        self.store = Store(self.tmp.name, 'test')
        self.fault(410, lambda: self.call(1, {'x': 1}))
        self.assertEqual('next', self.call(2, 'next'))
        self.assertEqual(2, self.calls)

    def test_bounded_current_records_and_bytes(self):
        for slot in range(16):
            for seq in range(1, 21):
                self.call(seq, 'x' * (RECEIPT_RESULT_BYTES - 2), slot)
        records = self.store.execute(lambda tx: tx.rows('_receipt_stream'), read_only=True)
        self.assertEqual(16, len(records))
        self.assertTrue(all(len(encode(r).encode()) < RECEIPT_RESULT_BYTES + 200 for r in records))
        self.assertEqual([], self.store.execute(lambda tx: tx.rows('_receipt'), read_only=True))

    def test_large_result_and_unicode_never_reexecute(self):
        value = '\u2603' * RECEIPT_RESULT_BYTES
        self.assertEqual(value, self.call(1, value))
        self.fault(410, lambda: self.call(1, value))
        self.assertEqual(1, self.calls)
        records = self.store.execute(lambda tx: tx.rows('_receipt_stream'), read_only=True)
        self.assertNotIn('result', records[0])
        self.call(2, 'acknowledged')

    def test_slots_and_actors_are_independent(self):
        self.call(1, 'a')
        self.call(1, 'b', slot=1)
        self.call(1, 'c', ctx=Context(None, actor='other'))
        self.assertEqual(3, self.calls)
        denied = Context(None, permissions=[])
        self.fault(403, lambda: self.call(1, 'a', ctx=denied))

    def test_invalid_envelopes_and_failed_mutation_do_not_advance(self):
        for request in ({}, {'stream': 0}, {'stream': 0, 'sequence': 1, 'extra': 2},
                        {'stream': True, 'sequence': 1}, {'stream': -1, 'sequence': 1},
                        {'stream': 16, 'sequence': 1}, {'stream': 0, 'sequence': True},
                        {'stream': 0, 'sequence': 0}, {'stream': 0, 'sequence': 2**63}):
            self.fault(400, lambda: self.store.execute(lambda tx: command(
                tx, self.ctx, 'test.receipt', {'value': 1}, request)))
        self.fault(400, lambda: self.store.execute(lambda tx: command(
            tx, self.ctx, 'test.receipt', {}, {'stream': 0, 'sequence': 1})))
        self.call(1)
        self.assertEqual(1, self.calls)

    def test_legacy_receipts_remain_unchanged(self):
        def legacy(value):
            return self.store.execute(lambda tx: command(
                tx, self.ctx, 'test.receipt', {'value': value}, 'legacy'))
        self.assertEqual('old', legacy('old'))
        for seq in range(1, 6):
            self.call(seq, seq)
        self.assertEqual('old', legacy('old'))
        self.fault(409, lambda: legacy('different'))
        self.assertEqual(6, self.calls)

    def test_shared_replay_and_lost_acknowledgement(self):
        journal = Journal()
        self.store.remote = journal
        original = journal.append
        def lost(version, entry):
            original(version, entry)
            raise Fault('Lost acknowledgement', 503)
        journal.append = lost
        self.fault(503, lambda: self.call(1, 'committed'))
        journal.append = original
        self.assertEqual('committed', self.call(1, 'committed'))
        self.assertEqual(1, self.calls)
        self.call(2, 'new')
        mirror = Store(self.tmp.name, 'mirror', remote=journal)
        self.fault(410, lambda: self.call(1, 'committed', store=mirror))
        self.assertEqual('new', self.call(2, 'new', store=mirror))
        self.assertEqual(2, self.calls)


    def test_failed_transaction_rolls_back_domain_and_floor(self):
        def fail(tx, ctx, value):
            tx.put('current', 'value', value)
            raise Fault('Failed mutation', 409)
        with patch.dict(OPERATIONS, {'test.fail': (fail, {
                'value': {'type': 'any', 'required': True}}, True)}):
            self.fault(409, lambda: self.store.execute(lambda tx: command(
                tx, self.ctx, 'test.fail', {'value': 'bad'}, {'stream': 0, 'sequence': 1})))
        self.assertIsNone(self.store.execute(lambda tx: tx.get('current', 'value'), read_only=True))
        self.call(1, 'good')

    def test_concurrent_shared_duplicate_commits_once(self):
        journal = Journal()
        self.store.remote = journal
        second = Store(self.tmp.name, 'second', remote=journal)
        barrier = threading.Barrier(2, timeout=5)
        original = journal.append
        def append(version, entry):
            if version == '0':
                barrier.wait()
            return original(version, entry)
        journal.append = append
        results, errors = [], []
        def write(store):
            try:
                results.append(self.call(1, 'once', store=store))
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=write, args=(store,))
                   for store in (self.store, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual([], errors)
        self.assertEqual(['once', 'once'], results)
        self.assertEqual(1, len(journal.entries))
        # Callbacks may run speculatively on CAS losers. Only one commits.
        self.assertEqual('once', second.execute(lambda tx: tx.get('current', 'value'), read_only=True))


    def test_authenticated_application_envelope_binds_stream_id(self):
        from biomerte.app import Application, initialize
        from biomerte.identity import sign
        home = os.path.join(self.tmp.name, 'application')
        peer_home = os.path.join(self.tmp.name, 'peer')
        initialize(home)
        initialize(peer_home)
        app, peer = Application(home), Application(peer_home)
        app.call('peer.trust', {'public_key': peer.config['public_key'],
                 'label': 'receipt test', 'scopes': ['test.receipt']}, 'trust')
        request = {'stream': 0, 'sequence': 1}
        args = {'value': 'signed'}
        envelope = {'site': app.site, 'realm': 'site', 'operation': 'test.receipt',
                    'arguments': args, 'id': request}
        auth = {'actor': peer.node, 'signature': sign(peer.key, envelope)}
        self.assertEqual('signed', app.call('test.receipt', args, request, auth=auth))
        self.assertEqual('signed', app.call('test.receipt', args, request, auth=auth))
        with self.assertRaises(Fault):
            app.call('test.receipt', args, {'stream': 0, 'sequence': 2}, auth=auth)
        self.assertEqual(1, self.calls)


    def test_future_legacy_payload_retirement_keeps_replay_fence(self):
        from biomerte.store import digest
        def call():
            return self.store.execute(lambda tx: command(
                tx, self.ctx, 'test.receipt', {'value': 'original'}, 'retire'))
        self.assertEqual('original', call())
        key = digest(['admin', 'retire'])
        def retire(tx):
            receipt = tx.get('_receipt', key)
            self.assertEqual(2, receipt['format'])
            self.assertEqual('test.receipt', receipt['operation'])
            self.assertIn('created_at', receipt)
            receipt.pop('result')
            receipt['result_expired'] = True
            tx.put('_receipt', key, receipt)
        self.store.execute(retire)
        self.fault(410, call)
        self.assertEqual(1, self.calls)


if __name__ == '__main__':
    unittest.main()
