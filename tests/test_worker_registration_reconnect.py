import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock
from biomerte.store import digest, encode
from biomerte.worker import Worker, run_worker


class FinishedProbe(Exception):
    pass


class RegistrationRecovery(unittest.TestCase):
    def test_unanswered_old_receipt_does_not_delay_fresh_fence(self):
        old = dict(id='old', provider='enrolled-provider', contract='sample:1', inputs={},
                   resource='slot', generation=1, lease='old-lease', state='succeeded', result={'ok': True})
        signature = digest([old[k] for k in ('contract', 'inputs', 'resource', 'generation', 'lease')])
        offer = dict(contract=old['contract'], resource=old['resource'])
        receipt_started, allow_receipt = threading.Event(), threading.Event()
        fresh_fenced, stopped, retired = threading.Event(), threading.Event(), threading.Event()

        class Remote:
            node, site, realm = old['provider'], 'test-site', 'site'

            def call(self, name, args, rid=None):
                if name == 'cap.status':
                    receipt_started.set()
                    if allow_receipt.wait(5):
                        return old
                    raise AssertionError('fixture did not release the historical receipt')
                if name == 'provider.poll':
                    if stopped.is_set():
                        raise FinishedProbe
                    fences = [] if fresh_fenced.is_set() else [dict(id='fresh-lease', resource='slot', generation=2)]
                    return dict(fences=fences, actions=[], activations=[])
                if name == 'cap.fenced':
                    self.fence = args
                    fresh_fenced.set()
                    return {}
                if name == 'cap.offer':
                    return {}
                raise AssertionError(name)

        original_retire = Worker.retire_result

        def record_retirement(worker, *args, **kwargs):
            result = original_retire(worker, *args, **kwargs)
            if result:
                retired.set()
            return result

        remote = Remote()
        with tempfile.TemporaryDirectory() as path, ThreadPoolExecutor(max_workers=1) as pool:
            seed = Worker(str(Path(path) / 'worker.sqlite'), remote, [offer])
            try:
                seed.db.execute('INSERT INTO actions VALUES(?,?,?,?,?)',
                                (old['id'], signature, old['state'], encode(old['result']), old['resource']))
                seed.db.execute('INSERT INTO result_retention_eligible VALUES(?,?)', (old['id'], signature))
                with mock.patch.object(Worker, 'retire_result', record_retirement):
                    process = pool.submit(run_worker, path, remote, dict(offers=[offer], poll_seconds=.01), False)
                    try:
                        self.assertTrue(receipt_started.wait(2))
                        self.assertTrue(fresh_fenced.wait(1), 'Historical receipt blocked fresh provider polling')
                        self.assertEqual(dict(provider=remote.node, lease='fresh-lease', generation=2), remote.fence)
                        self.assertIsNone(seed.db.execute('SELECT id FROM result_acknowledged').fetchone())
                        allow_receipt.set()
                        self.assertTrue(retired.wait(2))
                        self.assertEqual((old['id'],), seed.db.execute('SELECT id FROM result_acknowledged').fetchone())
                    finally:
                        allow_receipt.set()
                        stopped.set()
                        with self.assertRaises(FinishedProbe):
                            process.result(timeout=5)
            finally:
                seed.db.close()

    def test_partial_registration_keeps_serving_inbox_and_retries_only_missing_offer(self):
        events = []
        failed = False

        class Remote:
            node = 'enrolled-provider'
            site, realm = 'test-site', 'site'

            def call(self, name, args, rid=None):
                nonlocal failed
                contract = args['contract']
                events.append((contract, rid))
                if contract == 'second:1' and not failed:
                    failed = True
                    raise RuntimeError('The shared controller runtime is temporarily unavailable')

        def poll(worker, pool, **kwargs):
            events.append(('inbox', None))
            if sum(name == 'inbox' for name, _ in events) == 1:
                raise RuntimeError('Managed controller is busy')
            if sum(name == 'inbox' for name, _ in events) == 5:
                raise FinishedProbe
            return 0

        offers = [dict(contract=name + ':1', resource=name) for name in ('first', 'second', 'third')]
        with tempfile.TemporaryDirectory() as path, mock.patch.object(Worker, 'once', poll), \
                mock.patch('biomerte.worker.time.sleep'), self.assertRaises(FinishedProbe):
            run_worker(path, Remote(), dict(offers=offers, retry_seconds=0), False)
        attempts = [(name, rid) for name, rid in events if name != 'inbox']
        self.assertEqual([name for name, _ in attempts], ['first:1', 'second:1', 'third:1', 'second:1'])
        self.assertTrue(attempts[1][1])
        self.assertEqual(attempts[1][1], attempts[3][1])
        self.assertLess(events.index(('inbox', None)), events.index(attempts[1]))

    def test_one_shot_preserves_registration_then_poll_for_legacy_remote(self):
        events = []
        class Remote:
            node, site, realm = 'provider', 'test-site', 'site'
            def call(self, name, args):
                events.append(args['contract'])

        offers = [dict(contract=name, resource='r') for name in ('first', 'second')]
        with tempfile.TemporaryDirectory() as path, mock.patch.object(
                Worker, 'once', lambda worker, pool: events.append('inbox')):
            self.assertEqual(0, run_worker(path, Remote(), dict(offers=offers), True))
        self.assertEqual(['first', 'second', 'inbox'], events)

    def test_unrelated_runtime_error_keeps_its_original_failure(self):
        class Remote:
            node, site, realm = 'provider', 'test-site', 'site'
            def call(self, name, args):
                raise RuntimeError('Unexpected gateway origin')

        with tempfile.TemporaryDirectory() as path, self.assertRaisesRegex(RuntimeError, 'Unexpected gateway origin'):
            run_worker(path, Remote(), dict(offers=[dict(contract='first', resource='r')]), False)
