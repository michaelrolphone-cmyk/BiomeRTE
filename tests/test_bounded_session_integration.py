import json
import os
import tempfile
import unittest
from biomerte.store import Store, encode


class CombinedIndexReadiness(unittest.TestCase):
    def test_both_index_interfaces_read_dirty_rows_without_writer_access(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory, 'state')
            row = {'id': 'lease', 'state': 'bound', 'task': 'server'}
            store.execute(lambda tx: tx.put('leases', 'lease', row))
            def inspect(tx):
                self.assertEqual([row], tx.matching('leases', ('bound',)))
                self.assertEqual([row], tx.matching('leases', 'task', ('server',)))
                self.assertEqual([row], tx.matching('leases', 'state', ('bound',)))
            store.execute(inspect, read_only=True)
            row['state'] = 'fencing'
            with store.connect() as db:
                db.execute('UPDATE records SET value=? WHERE kind=? AND id=?',
                           (encode(row), 'leases', 'lease'))
                db.commit()
            def changed(tx):
                self.assertEqual([], tx.matching('leases', ('bound',)))
                self.assertEqual([row], tx.matching('leases', ('fencing',)))
                self.assertEqual([row], tx.matching('leases', 'state', ('fencing',)))
                self.assertEqual('@dirty', tx.db.execute(
                    'SELECT state FROM record_lifecycle WHERE kind=? AND id=?',
                    ('leases', 'lease')).fetchone()[0])
            store.execute(changed, read_only=True)
