"""An unavailable slot must not stall independent slots' admission/work."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from biomerte.worker import Worker, ActionPool
from biomerte.store import digest, encode

class SlowRemote:
    site = 'site'
    realm = 'site'
    node = 'provider'
    def __init__(self):
        self.release = threading.Event()
        self.entered = threading.Event()
        self.acknowledged = threading.Event()
        self.queue = {'activations': [], 'fences': [], 'actions': []}
        self.old = dict(id='old', provider=self.node, contract='terminal:1', inputs={}, resource='terminal', guard='slot-a', generation=1, lease='prior', state='uncertain', result={})
    def call(self, name, arguments, rid=None):
        if name == 'provider.poll':
            return self.queue
        if name == 'cap.status':
            self.entered.set()
            self.release.wait(2)
            return self.old
        if name == 'cap.fenced':
            self.acknowledged.set()
            return {}
        raise AssertionError(name)

class ParallelFenceLiveness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.remote = SlowRemote()
        self.worker = Worker(str(Path(self.temp.name) / 'worker.sqlite'), self.remote, [{'resource': 'terminal', 'contract': 'terminal:1', 'argv': ['/bin/true']}], None)
        a = self.remote.old
        self.worker.db.execute('INSERT INTO actions VALUES(?,?,?,?,?)', ('old', digest([a['contract'], a['inputs'], a['resource'], a['generation'], a['lease']]), 'uncertain', encode({}), 'slot-a'))
        self.pool = ActionPool(self.worker, 2)
    def tearDown(self):
        self.remote.release.set()
        self.pool.executor.shutdown(wait=True)
        self.worker.db.close()
        self.temp.cleanup()
    def fence(self, guard, name):
        return dict(id=name, resource='terminal', guard=guard, generation=2)
    def test_slow_fence_does_not_block_independent_action(self):
        self.remote.queue['fences'] = [self.fence('slot-a', 'blocked')]
        self.remote.queue['actions'] = [dict(id='independent', resource='terminal', guard='slot-b')]
        seen = threading.Event()
        with mock.patch.object(Worker, 'perform', side_effect=lambda a: seen.set()):
            start = time.monotonic()
            self.worker.once(self.pool)
            self.assertLess(time.monotonic() - start, .5, 'Fence lookup blocked the shared poller')
            self.assertTrue(self.remote.entered.wait(.5))
            self.assertTrue(seen.wait(.5))
    def test_slow_fence_does_not_block_independent_fence(self):
        self.remote.queue['fences'] = [self.fence('slot-a', 'blocked'), self.fence('slot-b', 'ready')]
        start = time.monotonic()
        self.worker.once(self.pool)
        self.assertLess(time.monotonic() - start, .5)
        self.assertTrue(self.remote.entered.wait(.5))
        self.assertTrue(self.remote.acknowledged.wait(.5))
    def test_action_never_bypasses_its_pending_fence(self):
        self.remote.queue['fences'] = [self.fence('slot-a', 'blocked')]
        self.remote.queue['actions'] = [dict(id='same-slot', resource='terminal', guard='slot-a')]
        seen = []
        with mock.patch.object(Worker, 'perform', side_effect=lambda a: seen.append(a['id'])):
            start = time.monotonic()
            self.worker.once(self.pool)
            self.assertLess(time.monotonic() - start, .5)
            self.assertTrue(self.remote.entered.wait(.5))
            self.assertEqual(seen, [])

    def test_reconciled_fence_completes_before_action_and_guard_is_reusable(self):
        self.remote.old['state'] = 'failed'
        self.remote.old['result'] = {'evidence': {'quiescent': True}}
        fence = self.fence('slot-a', 'recovered')
        self.remote.queue['fences'] = [fence]
        action = dict(id='after-recovery', resource='terminal', guard='slot-a')
        self.remote.queue['actions'] = [action]
        seen = []
        def perform(value):
            seen.append((value['id'], self.remote.acknowledged.is_set()))
        with mock.patch.object(Worker, 'perform', side_effect=perform):
            self.worker.once(self.pool)
            self.assertTrue(self.remote.entered.wait(.5))
            self.assertEqual(seen, [])
            self.remote.release.set()
            self.pool.running['slot-a'].result(1)
            self.remote.queue['fences'] = []
            self.worker.once(self.pool)
            self.pool.running['slot-a'].result(1)
            self.remote.queue['actions'] = []
            self.worker.once(self.pool)
        self.assertEqual(seen, [('after-recovery', True)])
        self.assertEqual(self.pool.running, {})
        self.assertEqual(self.pool.items, {})
        self.assertEqual(self.pool.failures, {})
        row = self.worker.db.execute("SELECT state FROM actions WHERE id='old'").fetchone()
        self.assertEqual(row, ('failed',))
