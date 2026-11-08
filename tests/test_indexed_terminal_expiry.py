"""Real controller, indexed retention and worker recovery, not summary fakes."""
import base64
import json
import os
import sys
import time
import unittest
from biomerte import retention
from biomerte.store import Fault, digest, uid
from biomerte.worker import Worker
from tests import test_runtime as runtime


class ControllerRemote:
    def __init__(self, app):
        self.app, self.node, self.site, self.realm = app, 'terminal-test', app.site, 'site'
        self.lose_ack = True
        self.offline = False
        self.results = []

    def call(self, name, args, request_id=None):
        if self.offline:
            raise OSError('Provider disconnected after lost acknowledgement')
        result = self.app.call(name, args, request_id or uid())
        if name == 'provider.result':
            self.results.append(dict(args))
            if self.lose_ack:
                self.lose_ack = False
                self.offline = True
                raise OSError('Committed terminal result, acknowledgement lost')
        return result


class IndexedTerminalExpiry(unittest.TestCase):
    setUp = runtime.Runtime.setUp
    call = runtime.Runtime.call

    def execute_terminal(self):
        self.effect = os.path.join(self.tmp.name, 'effect-count')
        self.output = b'x' * 160000
        artifact = self.call('artifact.put', {'data': base64.b64encode(self.output).decode(), 'transient': True})
        self.artifact = artifact['sha256']
        self.call('cap.define', {'name': 'test.terminal', 'inputs': {'argv': 'list'},
                               'outputs': {'stdout': 'str', 'sha256': 'str', 'evidence': 'obj'}})
        adapter = ('import sys,json,subprocess,hashlib; a=json.load(sys.stdin); '
                   'out=subprocess.check_output(a["inputs"]["argv"]); '
                   'print(json.dumps({"stdout":out.decode(),"sha256":hashlib.sha256(out).hexdigest(),'
                   '"evidence":{"quiescent":True}}))')
        self.offers = [{'contract': 'test.terminal:1', 'resource': 'terminal.slot',
                        'argv': [sys.executable, '-c', adapter]}]
        self.remote = ControllerRemote(self.a)
        self.worker_path = os.path.join(self.tmp.name, 'worker.sqlite')
        self.worker = Worker(self.worker_path, self.remote, self.offers)
        self.addCleanup(lambda: self.worker.db.close())
        self.worker.advertise()
        lease = self.call('cap.acquire', {'contract': 'test.terminal:1', 'task': 'terminal-debug'})
        argv = [sys.executable, '-c', 'import sys;open(sys.argv[1],"a").write("effect\\n");sys.stdout.write("x"*160000)', self.effect]
        self.action = self.call('cap.invoke', {'lease': lease['id'], 'operation': 'terminal-effect', 'inputs': {'argv': argv}})
        self.worker.once()
        self.worker.once()
        self.assertEqual(self.call('cap.status', {'operation': 'terminal-effect'})['state'], 'succeeded')
        self.assertEqual(len(self.remote.results), 1)
        self.assertFalse(self.worker.db.execute('SELECT 1 FROM result_acknowledged').fetchone())
        self.call('cap.release', {'lease': lease['id']})

    def expire(self):
        store = self.a.stores['site']
        # Exercise real bounded index catch-up and multiple candidate pages.
        for attempt in range(200):
            store.execute(lambda tx: retention.sweep(tx, now=time.time() + 100, seconds=1, keep=0))
            summary = store.execute(lambda tx: tx.get('actions', 'terminal-effect'), read_only=True)
            artifact = store.execute(lambda tx: tx.get('artifacts', self.artifact), read_only=True)
            if summary.get('result_expired') and artifact is None:
                self.assertGreater(attempt, 0)  # output exceeds a single indexing budget
                self.remote.offline = False
                return summary
        self.fail('Completed terminal output/artifact did not expire after bounded indexing')

    def assert_once(self):
        with open(self.effect) as stream:
            self.assertEqual(stream.read(), 'effect\n')

    def test_lost_ack_exact_redelivery_after_indexed_output_expiry(self):
        self.execute_terminal()
        summary = self.expire()
        args = self.remote.results[0]
        self.assertEqual(summary['result_digest'], digest(args['result']))
        self.assertNotIn('stdout', summary)
        acknowledged = self.remote.call('provider.result', args)
        self.assertTrue(acknowledged['result_expired'])
        signature = self.worker.db.execute('SELECT signature FROM actions WHERE id=?', ('terminal-effect',)).fetchone()[0]
        self.assertTrue(self.worker.retire_result('terminal-effect', signature, 'succeeded', args['result']))
        for changed in (dict(args, generation=args['generation'] + 1), dict(args, result={'different': True})):
            with self.assertRaises(Fault) as error:
                self.remote.call('provider.result', changed)
            self.assertEqual(error.exception.status, 409)
        self.worker.db.close()
        self.worker = Worker(self.worker_path, self.remote, self.offers)
        self.worker.once()
        self.assert_once()
        with self.assertRaises(Fault) as error:
            self.call('cap.invoke', {'lease': self.action['lease'], 'operation': 'terminal-effect', 'inputs': self.action['inputs']})
        self.assertEqual(error.exception.status, 410)

    def test_uncertain_worker_reconciles_actual_expired_controller_summary(self):
        self.execute_terminal()
        # Represents a disconnected worker restored to its pre-result uncertain
        # checkpoint, while the controller retained the committed outcome.
        self.worker.db.execute("UPDATE actions SET state='uncertain',result=? WHERE id=?",
                               (json.dumps({'reason': 'recovered uncertain checkpoint'}), 'terminal-effect'))
        summary = self.expire()
        count = len(self.remote.results)
        self.assertTrue(self.worker.reconcile_guard(self.action.get('guard', self.action['resource'])))
        row = self.worker.db.execute('SELECT state,result FROM actions WHERE id=?', ('terminal-effect',)).fetchone()
        self.assertEqual(row[0], 'succeeded')
        self.assertEqual(json.loads(row[1]), summary['recovery_result'])
        self.assertTrue(self.worker.db.execute('SELECT 1 FROM result_acknowledged WHERE id=?', ('terminal-effect',)).fetchone())
        self.worker.db.close()
        self.worker = Worker(self.worker_path, self.remote, self.offers)
        self.worker.once()
        self.assertEqual(len(self.remote.results), count)  # no shortened result is resubmitted
        self.assert_once()

    def test_successful_unacknowledged_local_result_retires_on_ordinary_poll(self):
        self.execute_terminal()
        self.expire()
        count = len(self.remote.results)
        self.worker.db.close()
        self.worker = Worker(self.worker_path, self.remote, self.offers)
        self.worker.once()
        row = self.worker.db.execute('SELECT state,result FROM actions WHERE id=?', ('terminal-effect',)).fetchone()
        self.assertEqual(row, ('succeeded', '{}'))
        self.assertTrue(self.worker.db.execute('SELECT 1 FROM result_acknowledged WHERE id=?', ('terminal-effect',)).fetchone())
        self.assertEqual(len(self.remote.results), count)
        self.assert_once()
