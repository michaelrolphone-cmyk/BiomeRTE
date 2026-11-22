import sqlite3
import tempfile
import unittest
from pathlib import Path

from biomerte.store import digest, encode
from biomerte.worker import reconcile_receipt


class ReconcileTerminalAck(unittest.TestCase):
    def test_lost_reply_to_committed_result_retires_local_uncertainty(self):
        outcome = {'exit_code': 1, 'effects_unknown': True, 'replay_allowed': False}
        action = dict(id='original', contract='host.terminal.run:1', inputs={'argv': ['false']},
                      resource='terminal.one', generation=21, lease='reservation',
                      provider='provider', state='failed', result=outcome)
        signature = digest([action[k] for k in ('contract', 'inputs', 'resource', 'generation', 'lease')])
        class ReadOnlyRemote:
            node = 'provider'
            def call(self, operation, args):
                if operation != 'cap.status':
                    raise AssertionError('exact accepted outcome must not require a second write')
                return action

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'worker.sqlite'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE actions(id TEXT PRIMARY KEY, signature TEXT, state TEXT, result TEXT)')
                db.execute('INSERT INTO actions VALUES(?,?,?,?)', ('original', signature, 'uncertain', '{}'))
            result = reconcile_receipt(directory, ReadOnlyRemote(), 'original', 'failed', outcome)
            self.assertEqual(result, action)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT state,result FROM actions').fetchone(),
                                 ('failed', encode(outcome)))
