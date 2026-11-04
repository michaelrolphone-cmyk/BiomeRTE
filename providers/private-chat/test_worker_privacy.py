"""Disk retention and interrupted-request behavior for the private worker."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.modules.setdefault('mac_provider', types.SimpleNamespace())
from private_chat import PrivateWorker
from biomerte.store import digest

class Remote:
    site, realm, node = 'test-site', 'site', 'test-provider'
    def __init__(self):
        self.results = []
    def call(self, operation, args, rid=None):
        if operation == 'provider.claim':
            return {'state':'claimed'}
        if operation == 'provider.result':
            self.results.append(args)
            return args
        raise AssertionError(operation)
class PrivateLedgerTests(unittest.TestCase):
    def action(self):
        return dict(id='00000000-0000-4000-8000-000000000001',
                    contract='chat.private.exchange:1', resource='privatechat.test',
                    lease='test-lease', generation=1, inputs={'envelope':{'ct':'request-cipher-canary'}})
    def worker(self, directory):
        remote = Remote()
        adapter = [sys.executable, '-c', 'import json,sys;json.load(sys.stdin);json.dump({"envelope":{"ct":"response-cipher-canary"}},sys.stdout)']
        worker = PrivateWorker(str(Path(directory)/'worker.sqlite'), remote,
                               [dict(contract='chat.private.exchange:1',resource='privatechat.test',argv=adapter)], None,
                               handler=lambda action: {'envelope':{'ct':'response-cipher-canary'}})
        worker.db.execute('INSERT INTO fences VALUES(?,?,?)',('privatechat.test',1,'test-lease'))
        return worker, remote
    def test_response_never_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            worker, remote = self.worker(directory)
            worker.perform(self.action())
            self.assertEqual(remote.results[0]['result']['envelope']['ct'],'response-cipher-canary')
            self.assertEqual(worker.db.execute('SELECT state,result FROM actions').fetchone(),('succeeded','{}'))
            worker.db.close()
            stored = (Path(directory)/'worker.sqlite').read_bytes()
            self.assertNotIn(b'response-cipher-canary',stored)
            self.assertNotIn(b'request-cipher-canary',stored)
    def test_interruption_fails_without_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            worker, remote = self.worker(directory)
            a = self.action()
            signature=digest([a['contract'],a['inputs'],a['resource'],a['generation'],a['lease']])
            worker.db.execute('INSERT INTO actions VALUES(?,?,?,?,?)',(a['id'],signature,'uncertain','{}',a['resource']))
            worker.perform(a)
            self.assertEqual(remote.results[0]['state'],'failed')
            self.assertNotIn('envelope',remote.results[0]['result'])
            worker.db.close()
    def test_lost_completion_ack_recovers_without_result_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            worker, remote = self.worker(directory)
            a=self.action();a.update(provider=remote.node,state='succeeded')
            signature=digest([a['contract'],a['inputs'],a['resource'],a['generation'],a['lease']])
            worker.db.execute('INSERT INTO actions VALUES(?,?,?,?,?)',(a['id'],signature,'uncertain','{}',a['resource']))
            remote.call=lambda operation,args,rid=None: a if operation=='cap.status' else self.fail('unexpected replay')
            worker.reconcile()
            self.assertEqual(worker.db.execute('SELECT state,result FROM actions').fetchone(),('succeeded','{}'))
            worker.db.close()
    def test_restart_fails_claimed_request_before_next_fence(self):
        with tempfile.TemporaryDirectory() as directory:
            worker, remote = self.worker(directory)
            a=self.action();a.update(provider=remote.node,state='claimed')
            signature=digest([a['contract'],a['inputs'],a['resource'],a['generation'],a['lease']])
            worker.db.execute('INSERT INTO actions VALUES(?,?,?,?,?)',(a['id'],signature,'uncertain','{}',a['resource']))
            original=remote.call
            remote.call=lambda op,args,rid=None: a if op=='cap.status' else original(op,args,rid)
            worker.reconcile()
            self.assertEqual(remote.results[0]['state'],'failed')
            self.assertEqual(worker.db.execute('SELECT state,result FROM actions').fetchone(),('failed','{}'))
            worker.db.close()
if __name__ == '__main__':
    unittest.main()
