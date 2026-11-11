import tempfile
import unittest
from unittest.mock import patch
from biomerte.api import Context, command, discover, op
from biomerte import providers
from biomerte.store import Store, Fault, digest
from tests.test_ha import Journal


class RefreshReceipts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name, 'refresh')
        self.ctx = Context(None)
        def seed(tx):
            tx.put('offers', 'offer', {'configuration': {'reservation_policy': {'idle_seconds': 60}}})
            tx.put('leases', 'lease', {'id': 'lease', 'offer': 'offer', 'state': 'bound', 'generation': 1})
        self.store.execute(seed)

    def call(self, rid):
        return self.store.execute(lambda tx: command(tx, self.ctx, 'cap.lease', {'lease': 'lease'}, rid))

    def test_many_poll_ids_keep_no_response_receipts_but_refresh_heartbeat(self):
        with patch('biomerte.providers.time.time', return_value=123):
            for number in range(100):
                self.assertEqual('bound', self.call('poll-' + str(number))['state'])
        self.assertEqual([], self.store.execute(lambda tx: tx.rows('_receipt'), read_only=True))
        self.assertEqual({'at': 123, 'generation': 1}, self.store.execute(
            lambda tx: tx.get('_reservation_activity', 'lease'), read_only=True))
        self.store.execute(lambda tx: tx.put('leases', 'lease', {'id': 'lease', 'state': 'released'}))
        self.assertEqual('released', self.call('poll-0')['state'])

    def test_existing_legacy_receipt_preserves_original_result_and_conflict(self):
        key = digest(['admin', 'old-poll'])
        old = {'signature': digest(['cap.lease', {'lease': 'lease'}]), 'result': {'state': 'queued'}}
        self.store.execute(lambda tx: tx.put('_receipt', key, old))
        self.assertEqual({'state': 'queued'}, self.call('old-poll'))
        with self.assertRaises(Fault):
            self.store.execute(lambda tx: command(tx, self.ctx, 'cap.lease', {'lease': 'other'}, 'old-poll'))
        self.assertEqual(old, self.store.execute(lambda tx: tx.get('_receipt', key), read_only=True))

    def test_policy_discovery_and_validation(self):
        spec = next(r for r in discover(None, self.ctx) if r['name'] == 'cap.lease')
        self.assertTrue(spec['write'])
        self.assertEqual('refresh', spec['receipt'])
        with self.assertRaises(ValueError):
            op('invalid', write=False, receipt='refresh')
        denied = Context(None, permissions=[])
        with self.assertRaises(Fault):
            self.store.execute(lambda tx: command(tx, denied, 'cap.lease', {'lease': 'lease'}, 'denied'))

    def test_heartbeat_is_replicated(self):
        journal = Journal()
        self.store.remote = journal
        with patch('biomerte.providers.time.time', return_value=456):
            self.call('fresh')
        self.assertEqual(1, len(journal.entries))
        changes = journal.entries[0]['changes']
        self.assertEqual([['_reservation_activity', 'lease', {'at': 456, 'generation': 1}]], changes)

    def test_explicit_stream_id_still_replays_exact_result(self):
        request = {'stream': 0, 'sequence': 1}
        old = self.call(request)
        self.store.execute(lambda tx: tx.put('leases', 'lease', {'id': 'lease', 'state': 'released'}))
        self.assertEqual(old, self.call(request))
