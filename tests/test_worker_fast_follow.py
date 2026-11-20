"""Real ActionPool retries must not masquerade as newly productive work (3.4+)."""
import copy
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import wait
from unittest import mock

from biomerte.store import Fault, digest, encode
from biomerte.worker import ActionPool, Worker, run_worker


class Remote:
    site = 'site'
    realm = 'site'
    node = 'worker'

    def __init__(self):
        self.queue = {'fences': [], 'actions': [], 'activations': []}
        self.polls = 0

    def call(self, name, arguments, rid=None):
        if name != 'provider.poll':
            raise AssertionError('Unexpected remote operation: ' + name)
        self.polls += 1
        return copy.deepcopy(self.queue)


class StopLoop(BaseException):
    pass


class WorkerFastFollowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.remote = Remote()
        self.worker = Worker(os.path.join(self.temporary.name, 'worker.sqlite'),
                             self.remote, [], None)
        self.pools = []
        self.gates = []
        self.condition = threading.Condition()
        self.calls = []
        self.responses = {}
        self.active = {}
        self.maximum_active = {}

        def perform(child, action):
            return self.execute(action, False)

        def fence(child, action):
            return self.execute(action, True)

        self.perform_patch = mock.patch.object(Worker, 'perform', perform)
        self.fence_patch = mock.patch.object(Worker, 'fence', fence)
        self.perform_patch.start()
        self.fence_patch.start()

    def tearDown(self):
        for gate in self.gates:
            gate.set()
        for pool in self.pools:
            pool.executor.shutdown(wait=True)
        self.fence_patch.stop()
        self.perform_patch.stop()
        self.worker.db.close()
        self.temporary.cleanup()

    def action(self, identifier='held', guard='slot-a', generation=7):
        return {'id': identifier, 'provider': self.remote.node,
                'contract': 'host.terminal.run:1', 'resource': 'terminal',
                'guard': guard, 'generation': generation,
                'lease': 'lease-' + str(generation), 'state': 'claimed',
                'inputs': {'argv': ['/bin/true']}}

    def pool(self, limit=2):
        pool = ActionPool(self.worker, limit)
        self.pools.append(pool)
        return pool

    def plan(self, action, outcomes, fence=False):
        key = ('fence' if fence else 'action', action['id'], action.get('generation'))
        self.responses[key] = list(outcomes)

    def execute(self, action, fence):
        kind = 'fence' if fence else 'action'
        guard = action.get('guard', action['resource'])
        key = (kind, action['id'], action.get('generation'))
        with self.condition:
            sequence = self.responses.get(key, [])
            outcome, gate = sequence.pop(0) if sequence else ('fail', None)
            self.calls.append((kind, action['id'], action.get('generation'), guard))
            self.active[guard] = self.active.get(guard, 0) + 1
            self.maximum_active[guard] = max(self.active[guard],
                                            self.maximum_active.get(guard, 0))
            self.condition.notify_all()
        try:
            if gate is not None and not gate.wait(10):
                raise AssertionError('Test did not release the controlled action')
            if outcome == 'fail':
                raise Fault('Runner requires reconciliation; retained effect', 409)
            if isinstance(outcome, BaseException):
                raise outcome
        finally:
            with self.condition:
                self.active[guard] -= 1
                self.condition.notify_all()

    def completed(self, pool):
        futures = list(pool.running.values())
        unused, pending = wait(futures, timeout=10)
        self.assertFalse(pending, 'Controlled worker attempt did not finish')

    def started(self, count):
        with self.condition:
            self.assertTrue(self.condition.wait_for(lambda: len(self.calls) >= count, 10))

    def retain_uncertainty(self, action):
        signature = digest([action[k] for k in
                            ('contract', 'inputs', 'resource', 'generation', 'lease')])
        self.worker.db.execute('INSERT INTO actions VALUES(?,?,?,?,?)',
                               (action['id'], signature, 'uncertain',
                                encode({'reason': 'retained uncertain execution'}),
                                action['guard']))
        self.worker.db.execute('INSERT INTO supervised_pending VALUES(?,?,?,?,?)',
                               (action['id'], signature, encode(action),
                                encode({'argv': ['/bin/true']}), encode({})))
        evidence = os.path.join(self.temporary.name, 'retained-execution-evidence')
        with open(evidence, 'w') as output:
            output.write('original effect remains unresolved')
        return evidence

    def ledger_snapshot(self):
        return (list(self.worker.db.execute('SELECT * FROM actions ORDER BY id')),
                list(self.worker.db.execute('SELECT * FROM supervised_pending ORDER BY id')),
                list(self.worker.db.execute('SELECT * FROM supervised_delivery ORDER BY id')),
                list(self.worker.db.execute('SELECT * FROM result_acknowledged ORDER BY id')))

    def test_same_failed_action_retries_without_fast_follow_and_keeps_evidence(self):
        action = self.action()
        evidence = self.retain_uncertainty(action)
        before = self.ledger_snapshot()
        pool = self.pool()
        self.assertEqual(self.worker.once(pool), 1)
        self.completed(pool)
        for expected_attempts in (2, 3, 4):
            self.assertEqual(self.worker.once(pool), 0)
            self.completed(pool)
            self.assertEqual(len(self.calls), expected_attempts)
        with open(os.path.join(self.temporary.name, 'worker-health.json')) as source:
            health = json.load(source)
        self.assertEqual(health['state'], 'degraded')
        self.assertEqual(health['pending'], 1)
        self.assertEqual(health['blocked'][0]['id'], action['id'])
        self.assertEqual(pool.failures[action['guard']]['generation'], 7)
        self.assertEqual(self.ledger_snapshot(), before)
        with open(evidence) as source:
            self.assertEqual(source.read(), 'original effect remains unresolved')

    def test_unrelated_guard_progresses_while_failed_action_retries(self):
        held, new = self.action(), self.action('new', 'slot-b')
        self.plan(new, [(None, None)])
        pool = self.pool()
        self.assertEqual(pool.dispatch([held]), 1)
        self.completed(pool)
        self.assertEqual(pool.dispatch([held, new]), 1)
        self.completed(pool)
        self.assertEqual([c[1] for c in self.calls].count('held'), 2)
        self.assertEqual([c[1] for c in self.calls].count('new'), 1)
        self.assertEqual(self.maximum_active, {'slot-a': 1, 'slot-b': 1})

    def test_new_action_identity_on_same_guard_requests_fast_follow(self):
        old, new = self.action(), self.action('replacement')
        pool = self.pool()
        self.assertEqual(pool.dispatch([old]), 1)
        self.completed(pool)
        self.assertEqual(pool.dispatch([new]), 1)
        self.completed(pool)
        self.assertEqual([c[1] for c in self.calls], ['held', 'replacement'])

    def test_new_generation_on_same_guard_requests_fast_follow(self):
        old, new = self.action(), self.action(generation=8)
        pool = self.pool()
        self.assertEqual(pool.dispatch([old]), 1)
        self.completed(pool)
        self.assertEqual(pool.dispatch([new]), 1)
        self.completed(pool)
        self.assertEqual([c[2] for c in self.calls], [7, 8])

    def test_new_fence_kind_takes_precedence_and_requests_fast_follow(self):
        action = self.action()
        lease = dict(action)
        pool = self.pool()
        self.assertEqual(pool.dispatch([action]), 1)
        self.completed(pool)
        self.assertEqual(pool.dispatch([action], fences=[lease]), 1)
        self.completed(pool)
        self.assertEqual([c[0] for c in self.calls], ['action', 'fence'])
        self.assertEqual(self.maximum_active['slot-a'], 1)

    def test_successful_retry_clears_failure_and_restores_fast_follow(self):
        action = self.action()
        self.plan(action, [('fail', None), (None, None), (None, None)])
        pool = self.pool()
        self.assertEqual(pool.dispatch([action]), 1)
        self.completed(pool)
        self.assertEqual(pool.dispatch([action]), 0)
        self.completed(pool)
        self.assertEqual(pool.dispatch([action]), 1)
        self.assertNotIn(action['guard'], pool.failures)
        self.completed(pool)
        self.assertEqual(len(self.calls), 3)

    def test_inflight_retry_keeps_guard_serialized_and_other_slot_available(self):
        held, other = self.action(), self.action('other', 'slot-b')
        gate = threading.Event()
        self.gates.append(gate)
        self.plan(held, [('fail', None), ('fail', gate)])
        self.plan(other, [(None, None)])
        pool = self.pool()
        self.assertEqual(pool.dispatch([held]), 1)
        self.completed(pool)
        self.assertEqual(pool.dispatch([held]), 0)
        self.started(2)
        self.assertEqual(pool.dispatch([held, other]), 1)
        self.started(3)
        self.assertEqual([c[1] for c in self.calls].count('held'), 2)
        self.assertEqual(self.maximum_active['slot-a'], 1)
        gate.set()
        self.completed(pool)

    def test_failed_fence_retries_before_action_without_false_fast_follow(self):
        lease, action = self.action('lease'), self.action('later')
        pool = self.pool()
        self.assertEqual(pool.dispatch([], fences=[lease]), 1)
        self.completed(pool)
        self.assertEqual(pool.dispatch([action], fences=[lease]), 0)
        self.completed(pool)
        self.assertEqual([(c[0], c[1]) for c in self.calls],
                         [('fence', 'lease'), ('fence', 'lease')])

    def test_transport_failure_remains_retryable_at_normal_poll_signal(self):
        action = self.action()
        self.plan(action, [(IOError('transport unavailable'), None),
                           (IOError('transport unavailable'), None)])
        pool = self.pool()
        self.assertEqual(pool.dispatch([action]), 1)
        self.completed(pool)
        self.assertEqual(pool.dispatch([action]), 0)
        self.completed(pool)
        self.assertEqual(len(self.calls), 2)
        self.assertIn('transport unavailable', pool.failures['slot-a']['reason'])

    def test_run_loop_uses_configured_poll_seconds_for_same_failed_work(self):
        self.remote.queue['actions'] = [self.action()]
        sleeps = []

        def make_pool(worker, limit):
            pool = ActionPool(worker, limit)
            self.pools.append(pool)
            return pool

        def after_cycle(seconds):
            sleeps.append(seconds)
            self.completed(self.pools[-1])
            if len(sleeps) == 2:
                raise StopLoop()

        with mock.patch('biomerte.worker.ActionPool', side_effect=make_pool):
            with mock.patch('biomerte.worker.time.sleep', side_effect=after_cycle):
                with self.assertRaises(StopLoop):
                    run_worker(self.temporary.name, self.remote,
                               {'offers': [], 'parallelism': 2, 'poll_seconds': 3.25}, False)
        self.assertEqual(sleeps, [0.1, 3.25])
        self.assertEqual(self.remote.polls, 2)
        self.assertEqual(len(self.calls), 2)


if __name__ == '__main__':
    unittest.main()
