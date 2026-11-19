"""Keep deployed recovery and stream fixes when adopting the main branch."""
import os
import tempfile
import unittest
from unittest.mock import patch
from biomerte.app import Application, initialize
from biomerte.store import Fault, uid, digest
from biomerte import command_runner, completion_receipts, discovery, supervised_commands
from tests import test_supervised_commands as runner_fixture
from tests import test_discovery_progress as discovery_fixture


class RuntimeReconciliation(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        initialize(self.directory.name)
        self.app = Application(self.directory.name)
        self.now = 10000.
        self.clock = patch('time.time', side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def call(self, op, **arguments):
        return self.app.call(op, arguments, uid())

    def completed_terminal(self):
        self.call('cap.define', name='host.terminal.run', inputs={'argv': 'list'}, outputs={'ok': 'bool'})
        self.call('cap.offer', provider='test', contract='host.terminal.run:1', resource='terminal')
        lease = self.call('cap.acquire', task='once', contract='host.terminal.run:1', resource='terminal')
        self.call('cap.fenced', provider='test', lease=lease['id'], generation=lease['generation'])
        self.call('cap.invoke', lease=lease['id'], operation='done', inputs={'argv': []})
        self.call('provider.claim', provider='test', operation='done', generation=lease['generation'])
        args = dict(provider='test', operation='done', generation=lease['generation'],
                    state='succeeded', result={'ok': True})
        completed = self.call('provider.result', **args)
        return args, completed

    def test_expired_output_still_acknowledges_exact_completion_without_replay(self):
        args, completed = self.completed_terminal()
        self.now = completed['expires_at'] + 1
        self.app.tick('site')
        receipt = self.call('cap.status', operation='done', recovery=True)
        self.assertTrue(receipt['acknowledged'])
        self.assertTrue(receipt['result_expired'])
        self.assertEqual(receipt['result_digest'], digest(args['result']))
        self.assertNotIn('result', receipt)
        self.assertEqual(self.call('provider.result', **args), receipt)
        with self.assertRaises(Fault):
            self.call('cap.status', operation='done')
        with self.assertRaises(Fault) as error:
            self.call('cap.submit', contract='host.terminal.run:1', resource='terminal',
                      operation='done', inputs={'argv': []})
        self.assertEqual(error.exception.status, 410)
        for change in ({'generation': args['generation'] + 1}, {'result': {'ok': False}},
                       {'provider': 'different'}):
            with self.assertRaises(Fault) as error:
                self.call('provider.result', **dict(args, **change))
            self.assertEqual(error.exception.status, 409)

    def test_completion_digest_has_fixed_expiry_and_unknown_is_not_success(self):
        args, completed = self.completed_terminal()
        self.now = completed['expires_at'] + completion_receipts.RETENTION + 1
        self.app.tick('site')
        result = self.call('cap.status', operation='done', recovery=True)
        self.assertEqual(result['state'], 'missing_unknown')
        self.assertFalse(result['acknowledged'])
        self.assertFalse(result['replay_allowed'])
        with self.assertRaises(Fault):
            self.call('provider.result', **args)

    def stream(self):
        self.app.config.update(stream_router_secret='a' * 64,
                               stream_gateway_url='wss://gateway/api/stream')
        self.call('cap.define', name='sample', inputs={}, outputs={})
        self.call('cap.offer', provider='test', contract='sample:1', resource='slot',
                  configuration={'lease_class': 'persistent',
                                 'stream': {'enabled': True, 'lanes': ['events'],
                                            'direction': 'bidirectional'}})
        first = self.call('cap.acquire', task='first', contract='sample:1', resource='slot')
        self.call('cap.fenced', provider='test', lease=first['id'], generation=first['generation'])
        grant = self.call('stream.open', lease=first['id'])
        return first, grant

    def test_refenced_stream_retires_only_obsolete_grant(self):
        first, grant = self.stream()
        def refence(tx):
            lease = tx.need('leases', first['id'])
            lease['generation'] += 1
            tx.put('leases', lease['id'], lease)
            guard = tx.need('guards', lease['guard'])
            guard['generation'] = lease['generation']
            tx.put('guards', lease['guard'], guard)
        self.app.stores['site'].execute(refence)
        renewed = self.call('stream.open', lease=first['id'])
        self.assertNotEqual(renewed['id'], grant['id'])
        self.assertEqual(renewed['generation'], grant['generation'] + 1)
        self.assertEqual(self.call('stream.open', lease=first['id'])['id'], renewed['id'])
        with self.assertRaises(Fault):
            self.call('stream.inspect', session=grant['id'])

    def test_original_stream_owner_can_rejoin_existing_subscriber(self):
        first, grant = self.stream()
        second = self.call('cap.acquire', task='second', contract='sample:1', resource='slot')
        self.call('stream.open', lease=second['id'])
        self.call('stream.close', session=grant['id'], lease=first['id'])
        self.assertEqual(self.call('stream.inspect', session=grant['id'])['consumers'], [second['id']])
        returned = self.call('cap.acquire', task='first', contract='sample:1', resource='slot')
        self.assertNotIn('shared_owner_released', returned)
        reopened = self.call('stream.open', lease=returned['id'])
        self.assertEqual(reopened['id'], grant['id'])
        self.assertEqual(reopened['epoch'], grant['epoch'])
        self.assertEqual(set(self.call('stream.inspect', session=grant['id'])['consumers']),
                         {first['id'], second['id']})

    def test_deleted_stream_lease_reports_superseded_authority(self):
        first, grant = self.stream()
        self.app.stores['site'].execute(lambda tx: tx.drop('leases', first['id']))
        with self.assertRaises(Fault) as error:
            self.call('stream.inspect', session=grant['id'])
        self.assertEqual(error.exception.status, 409)


class RunnerReconciliation(unittest.TestCase):
    setUp = runner_fixture.RunnerTests.setUp
    argv = runner_fixture.RunnerTests.argv
    worker = runner_fixture.RunnerTests.worker
    action = runner_fixture.RunnerTests.action
    text = runner_fixture.RunnerTests.text

    def test_completion_arriving_during_custody_check_is_not_interrupted(self):
        directory = command_runner.location(self.root, 'race')
        os.makedirs(directory)
        command_runner.save(os.path.join(directory, 'state.json'), {'state': 'running'})
        real_locked = command_runner.locked
        expected = {'state': 'finished', 'returncode': 0}
        def acquire(path):
            command_runner.save(os.path.join(directory, 'completion.json'), expected)
            return real_locked(path)
        with patch.object(command_runner, 'locked', side_effect=acquire):
            self.assertEqual(command_runner.inspect(directory), expected)

    def test_missing_authority_receipt_parks_outbox_and_retains_executor_evidence(self):
        class Missing(runner_fixture.Remote):
            def call(self, name, args, rid=None):
                if name == 'provider.result':
                    raise IOError('authoritative action no longer retained')
                if name == 'cap.status':
                    return {'id': args['operation'], 'state': 'missing_unknown',
                            'acknowledged': False, 'result_expired': True, 'replay_allowed': False}
                return super().call(name, args, rid)
        worker, unused = self.worker(Missing())
        try:
            worker.perform(self.action())
            self.assertEqual(supervised_commands.pending(worker), [])
            unresolved = supervised_commands.unresolved(worker)
            self.assertEqual(len(unresolved), 1)
            self.assertFalse(unresolved[0]['acknowledged'])
            self.assertFalse(unresolved[0]['replay_allowed'])
            directory = command_runner.location(os.path.join(self.root, 'command-runners'), 'command')
            self.assertTrue(os.path.exists(os.path.join(directory, 'stdout')))
            self.assertFalse(os.path.exists(os.path.join(directory, 'acknowledged.json')))
            self.assertEqual(self.text(os.path.join(self.root, 'effects')), 'once\n')
        finally:
            worker.db.close()


class DiscoveryReconciliation(unittest.TestCase):
    def test_explicit_item_limit_survives_cursor_and_is_not_a_byte_limit(self):
        rows = [{'id': str(i).zfill(3), 'description': 'x' * 1000} for i in range(60)]
        with patch.object(discovery.providers, 'discover', return_value={'contracts': rows, 'offers': []}):
            first = discovery.page(discovery_fixture.Tx(), None, section='contracts', limit=37)
            self.assertEqual(first['items'], rows[:37])
            second = discovery.page(discovery_fixture.Tx(), None, cursor=first['next_cursor'])
            self.assertEqual(second['items'], rows[37:])
            for limit in (0, -1, True, 1.5):
                with self.assertRaises(Fault):
                    discovery.page(discovery_fixture.Tx(), None, section='contracts', limit=limit)
