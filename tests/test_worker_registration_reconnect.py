import tempfile
import unittest
from unittest import mock
from biomerte.worker import Worker, run_worker


class FinishedProbe(Exception):
    pass


class RegistrationRecovery(unittest.TestCase):
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

        def poll(worker, pool):
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
