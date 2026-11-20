"""Frozen executor evidence reuses durable controller receipts without replay.

This suite also runs with the unchanged 09485891 controller modules and the
captured installed terminal worker. It does not rely on server-side no-ops.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from biomerte.app import Application, initialize
from biomerte import command_runner as runner
from biomerte import supervised_commands as supervised
from biomerte.store import Fault, digest, encode, uid
from biomerte.worker import Remote, Worker


class SupervisedPublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, 'controller')
        self.worker_path = os.path.join(self.tmp.name, 'worker', 'worker.sqlite')
        initialize(self.home)
        self.app = Application(self.home)
        self.addCleanup(self.close_app)
        self.now = 10000.
        self.clock = mock.patch('time.time', side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.calls = []
        self.lose_after = None
        self.fail_before = None
        self.crash_after = None
        self.transport = mock.patch('biomerte.worker.post', side_effect=self.post)
        self.transport.start()
        self.addCleanup(self.transport.stop)
        self.call('cap.define', name='host.terminal.run', inputs={}, outputs={'ok':'bool'})
        self.call('cap.offer', provider='provider', contract='host.terminal.run:1',
                  resource='terminal', configuration={'execution_custody':True}, capacity=1)
        self.lease = self.call('cap.acquire', task='fixture', contract='host.terminal.run:1',
                               resource='terminal')
        self.call('cap.fenced', provider='provider', lease=self.lease['id'],
                  generation=self.lease['generation'])
        self.action = self.call('cap.invoke', lease=self.lease['id'], operation='command', inputs={})
        self.offer = dict(contract='host.terminal.run:1', resource='terminal',
                          argv=[sys.executable, '-S', '-c', "raise AssertionError('must not relaunch')"])
        self.open_worker()
        self.addCleanup(lambda: self.worker.db.close())
        self.directory = runner.location(os.path.join(os.path.dirname(self.worker_path),
                                                      'command-runners'), self.action['id'])
        os.makedirs(self.directory)
        self.write_interrupted()

    def close_app(self):
        for store in self.app.stores.values():
            store.close()

    def open_worker(self):
        self.remote = Remote('http://127.0.0.1', self.app.site, 'provider', None,
                             token='isolated-fixture-token')
        self.worker = Worker(self.worker_path, self.remote, [self.offer], None)
        self.worker.db.execute('INSERT OR IGNORE INTO fences VALUES(?,?,?)',
                               (self.lease['guard'], self.lease['generation'], self.lease['id']))

    def call(self, op, **arguments):
        return self.app.call(op, arguments, uid(), 'site')

    def post(self, url, path, body, ca=None, token=None, timeout=30):
        # Exercise the actual Remote.call envelope and the real durable API.
        self.calls.append((body['operation'], body['id'], json.loads(encode(body['arguments']))))
        if body['operation'] == self.fail_before:
            self.fail_before = None
            raise IOError('transport failed before acceptance')
        reply = self.app.call(body['operation'], body['arguments'], body['id'], body['realm'])
        if body['operation'] == self.crash_after:
            os._exit(73)  # Acceptance committed; no response reached the worker.
        if body['operation'] == self.lose_after:
            self.lose_after = None
            raise IOError('response lost after durable acceptance')
        return reply

    def read(self, fn):
        return self.app.stores['site'].execute(fn, read_only=True)

    def snapshot(self):
        return self.read(lambda tx: (
            tx.need('actions', self.action['id']), tx.need('leases', self.lease['id']),
            tx.get('_meta', 'events', 0),
            tx.db.execute("SELECT COUNT(*) FROM records WHERE kind='_receipt'").fetchone()[0]))

    def reports(self, name):
        return [(rid, args) for operation, rid, args in self.calls if operation == name]

    def write_interrupted(self, observed=123., adapter_pid=456):
        runner.save(os.path.join(self.directory, 'state.json'),
                    dict(state='running', observed_at=observed, adapter_pid=adapter_pid))

    def perform_uncertain(self):
        with mock.patch('biomerte.command_runner.subprocess.Popen',
                        side_effect=AssertionError('uncertain execution was relaunched')):
            with self.assertRaises(Fault) as caught:
                self.worker.perform(self.action)
        self.assertEqual(409, caught.exception.status)
        self.assertIn('Runner requires reconciliation', str(caught.exception))

    def assert_evidence_held(self):
        self.assertEqual(['command'], [a['id'] for a in supervised.pending(self.worker)])
        self.assertEqual('uncertain', self.worker.db.execute(
            'SELECT state FROM actions WHERE id=?', ('command',)).fetchone()[0])
        self.assertEqual([], list(self.worker.db.execute('SELECT id FROM supervised_delivery')))
        self.assertEqual([], list(self.worker.db.execute('SELECT id FROM result_acknowledged')))
        self.assertFalse(os.path.exists(os.path.join(self.directory, 'acknowledged.json')))
        self.assertTrue(os.path.exists(os.path.join(self.directory, 'state.json')))

    def check_frozen_replay(self):
        self.perform_uncertain()
        self.now += 121
        self.app.tick('site')
        before = self.snapshot()
        self.assertIn('recovery', before[0])
        before_changes = self.worker.db.total_changes
        self.calls = []
        for unused in range(5):
            self.now += 1
            self.perform_uncertain()
        self.assertEqual(before, self.snapshot())
        self.assertEqual(before_changes, self.worker.db.total_changes)
        for operation in ('provider.custody', 'provider.result'):
            reports = self.reports(operation)
            self.assertEqual(5, len(reports))
            self.assertEqual(1, len(set(rid for rid, args in reports)))
            self.assertTrue(reports[0][0].startswith('supervised-'))
        self.assert_evidence_held()

    def test_interrupted_snapshot_replays_without_controller_or_worker_writes(self):
        self.check_frozen_replay()

    def test_finished_invalid_output_remains_uncertain_without_republication(self):
        runner.save(os.path.join(self.directory, 'completion.json'),
                    dict(state='finished', returncode=0, observed_at=456.))
        with open(os.path.join(self.directory, 'stdout'), 'w') as stream:
            stream.write('not a JSON result')
        self.check_frozen_replay()
        self.assertTrue(os.path.exists(os.path.join(self.directory, 'stdout')))
        self.assertEqual('finished', self.snapshot()[0]['custody']['state'])

    def test_lost_result_ack_survives_worker_and_controller_reopen(self):
        self.lose_after = 'provider.result'
        with mock.patch('biomerte.command_runner.subprocess.Popen',
                        side_effect=AssertionError('must not relaunch')):
            with self.assertRaises(IOError):
                self.worker.perform(self.action)
        first_ids = {name: self.reports(name)[-1][0]
                     for name in ('provider.custody', 'provider.result')}
        self.now += 121
        self.app.tick('site')
        before = self.snapshot()
        self.worker.db.close()
        self.close_app()
        self.app = Application(self.home)
        self.open_worker()
        changes = self.worker.db.total_changes
        self.calls = []
        self.perform_uncertain()
        self.assertEqual(before, self.snapshot())
        self.assertEqual(changes, self.worker.db.total_changes)
        for name, rid in first_ids.items():
            self.assertEqual(rid, self.reports(name)[-1][0])
        self.assert_evidence_held()

    def test_lost_custody_ack_retries_independently_of_uncertain_result(self):
        self.lose_after = 'provider.custody'
        self.perform_uncertain()
        first = {name: self.reports(name)[-1][0]
                 for name in ('provider.custody', 'provider.result')}
        before = self.snapshot()
        self.calls = []
        self.perform_uncertain()
        self.assertEqual(before, self.snapshot())
        for name, rid in first.items():
            self.assertEqual(rid, self.reports(name)[-1][0])
        self.assert_evidence_held()

    def test_unaccepted_first_result_is_retried_without_command_replay(self):
        self.fail_before = 'provider.result'
        with mock.patch('biomerte.command_runner.subprocess.Popen',
                        side_effect=AssertionError('must not relaunch')):
            with self.assertRaises(IOError):
                self.worker.perform(self.action)
        first_id = self.reports('provider.result')[-1][0]
        self.assertEqual('claimed', self.snapshot()[0]['state'])
        self.calls = []
        self.perform_uncertain()
        self.assertEqual(first_id, self.reports('provider.result')[-1][0])
        self.assertEqual('uncertain', self.snapshot()[0]['state'])
        self.assert_evidence_held()

    def test_changed_evidence_gets_new_receipts_and_later_completion_is_delivered(self):
        self.perform_uncertain()
        first_id = self.reports('provider.result')[-1][0]
        self.write_interrupted(observed=124., adapter_pid=457)
        self.calls = []
        self.perform_uncertain()
        changed_id = self.reports('provider.result')[-1][0]
        self.assertNotEqual(first_id, changed_id)
        self.assertEqual(457, self.snapshot()[0]['result']['runner']['last_observation']['adapter_pid'])
        before = self.snapshot()
        self.perform_uncertain()
        self.assertEqual(before, self.snapshot())
        runner.save(os.path.join(self.directory, 'completion.json'),
                    dict(state='finished', returncode=0, observed_at=125.))
        with open(os.path.join(self.directory, 'stdout'), 'w') as stream:
            json.dump({'ok':True}, stream)
        with mock.patch('biomerte.command_runner.subprocess.Popen',
                        side_effect=AssertionError('completion caused replay')):
            self.worker.perform(self.action)
        self.assertEqual('succeeded', self.snapshot()[0]['state'])
        self.assertEqual('released', self.snapshot()[1]['state'])
        self.assertEqual([], supervised.pending(self.worker))
        self.assertTrue(os.path.exists(os.path.join(self.directory, 'acknowledged.json')))

    def test_retired_receipt_is_not_treated_as_a_final_execution_ack(self):
        self.perform_uncertain()
        request_id = self.reports('provider.result')[-1][0]
        key = digest(['admin', request_id])
        def retire(tx):
            receipt = tx.need('_receipt', key)
            receipt['result_expired'] = True
            tx.put('_receipt', key, receipt)
        self.app.stores['site'].execute(retire)
        with self.assertRaises(Fault) as caught:
            self.worker.perform(self.action)
        self.assertEqual(410, caught.exception.status)
        self.assert_evidence_held()

    @unittest.skipUnless(hasattr(os, 'fork'), 'process crash test requires fork')
    def test_process_crash_after_acceptance_reuses_the_committed_receipt(self):
        self.worker.db.close()
        self.close_app()
        pid = os.fork()
        if pid == 0:
            try:
                self.app = Application(self.home)
                self.open_worker()
                self.crash_after = 'provider.result'
                with mock.patch('biomerte.command_runner.subprocess.Popen',
                                side_effect=AssertionError('must not relaunch')):
                    self.worker.perform(self.action)
            except BaseException:
                os._exit(74)
            os._exit(75)
        unused, status = os.waitpid(pid, 0)
        self.assertTrue(os.WIFEXITED(status))
        self.assertEqual(73, os.WEXITSTATUS(status))
        self.app = Application(self.home)
        self.open_worker()
        before = self.snapshot()
        changes = self.worker.db.total_changes
        self.perform_uncertain()
        self.assertEqual(before, self.snapshot())
        self.assertEqual(changes, self.worker.db.total_changes)
        self.assert_evidence_held()


if __name__ == '__main__':
    unittest.main()
