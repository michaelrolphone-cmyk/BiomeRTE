"""Uncertainty publication is evidence, never release or fresh live custody."""
import tempfile
import unittest
from unittest.mock import patch

from biomerte.app import Application, initialize
from biomerte.store import Fault, digest, uid


class PublicationFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        initialize(self.tmp.name)
        self.app = Application(self.tmp.name)
        self.addCleanup(lambda: [store.close() for store in self.app.stores.values()])
        self.now = 10000.
        self.clock = patch('time.time', side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.call('cap.define', name='host.terminal.run', inputs={}, outputs={'ok':'bool'})
        self.call('cap.offer', provider='provider', contract='host.terminal.run:1',
                  resource='slot', capacity=1, configuration={'execution_custody':True})
        self.lease = self.call('cap.acquire', task='fixture', contract='host.terminal.run:1',
                               resource='slot')
        self.call('cap.fenced', provider='provider', lease=self.lease['id'],
                  generation=self.lease['generation'])
        self.action = self.call('cap.invoke', lease=self.lease['id'], operation='command', inputs={})
        self.call('provider.claim', provider='provider', operation='command',
                  generation=self.lease['generation'])
        self.identity = digest([self.action[k] for k in
                                ('contract','inputs','resource','generation','lease')])

    def call(self, op, **args):
        return self.app.call(op, args, uid(), 'site')

    def read(self, fn):
        return self.app.stores['site'].execute(fn, read_only=True)

    def records(self):
        return self.read(lambda tx: (tx.need('actions', 'command'),
                                    tx.need('leases', self.lease['id'])))

    def events(self, topic):
        return self.read(lambda tx: [row for row in tx.rows('events')
                                     if row['topic'] == topic and row['payload'].get('id') == 'command'])

    def report(self, result, state='uncertain'):
        return self.call('provider.result', provider='provider', operation='command',
                         generation=self.lease['generation'], state=state, result=result)

    def custody(self, state='interrupted', **changes):
        observation = dict(state=state, signature=self.identity, recovery_path='/retained/command',
                           local_observed_at=123.)
        observation.update(changes)
        return self.call('provider.custody', provider='provider', operation='command',
                         generation=self.lease['generation'], observation=observation)

    def recover(self):
        self.now += 121
        self.app.tick('site')
        action, lease = self.records()
        self.assertIn('recovery', action)
        self.assertTrue(lease['recovery_required'])
        return action, lease


class UncertainResultIdempotence(PublicationFixture):
    def test_first_provider_report_is_not_hidden_by_tick_uncertainty(self):
        prior, lease = self.recover()
        payload = prior.get('result', {})
        before = len(self.events('operation.uncertain'))
        first = self.report(payload)
        self.assertEqual(before + 1, len(self.events('operation.uncertain')))
        self.assertEqual(prior['recovery'], first['recovery'])
        self.assertTrue(self.records()[1]['recovery_required'])
        self.assertEqual('bound', self.records()[1]['state'])

    def test_exact_fresh_id_replays_preserve_recovery_and_emit_nothing(self):
        self.recover()
        first = self.report({'reason':'lost original custodian'})
        before = self.records()
        events = self.events('operation.uncertain')
        recovery_events = self.events('operation.recovery_requested')
        for unused in range(5):
            self.now += 1
            self.assertEqual(first, self.report({'reason':'lost original custodian'}))
            self.app.tick('site')
        self.assertEqual(before, self.records())
        self.assertEqual(events, self.events('operation.uncertain'))
        self.assertEqual(recovery_events, self.events('operation.recovery_requested'))

    def test_changed_uncertain_evidence_is_recorded_once_without_resolving_recovery(self):
        prior, lease = self.recover()
        self.report({'reason':'lost runner', 'evidence':{'last_pid':1}})
        before = len(self.events('operation.uncertain'))
        changed = {'reason':'lost runner', 'evidence':{'last_pid':2}}
        reply = self.report(changed)
        self.assertEqual(changed, reply['result'])
        self.assertEqual(prior['recovery'], reply['recovery'])
        self.assertEqual(before + 1, len(self.events('operation.uncertain')))
        self.report(changed)
        self.assertEqual(before + 1, len(self.events('operation.uncertain')))
        self.assertTrue(self.records()[1]['recovery_required'])
        with self.assertRaises(Fault):
            self.call('cap.release', lease=self.lease['id'])

    def test_semantic_replay_still_checks_current_guard_and_provider_identity(self):
        self.recover()
        result = {'reason':'lost runner'}
        self.report(result)
        before = self.records()
        def supersede(tx):
            guard = tx.need('guards', self.lease['guard'])
            guard['generation'] += 1
            tx.put('guards', self.lease['guard'], guard)
        self.app.stores['site'].execute(supersede)
        with self.assertRaises(Fault) as caught:
            self.report(result)
        self.assertEqual(409, caught.exception.status)
        with self.assertRaises(Fault):
            self.call('provider.result', provider='other', operation='command',
                      generation=self.lease['generation'], state='uncertain', result=result)
        self.assertEqual(before, self.records())

    def test_real_quiescent_completion_resolves_recovery_and_releases(self):
        self.recover()
        self.report({'reason':'waiting for evidence'})
        self.custody('finished')
        reply = self.report({'ok':True}, state='succeeded')
        self.assertEqual('succeeded', reply['state'])
        self.assertNotIn('recovery', reply)
        self.assertNotIn('uncertain_result_digest', reply)
        self.assertNotIn('recovery_required', self.records()[1])
        self.assertEqual('released', self.records()[1]['state'])


class FrozenCustodyIdempotence(PublicationFixture):
    def test_frozen_exact_observations_keep_the_original_observation_time(self):
        for state in ('interrupted', 'uncertain', 'finished', 'not_started'):
            first = self.custody(state)
            before = self.records()
            self.now += 1
            self.assertEqual(first, self.custody(state))
            self.assertEqual(before, self.records())

    def test_new_frozen_observation_is_recorded_and_identity_is_still_checked(self):
        first = self.custody()
        self.now += 1
        changed = self.custody(local_observed_at=124., adapter_pid=2)
        self.assertGreater(changed['observed_at'], first['observed_at'])
        self.assertEqual(124., self.records()[0]['custody']['local_observed_at'])
        before = self.records()
        with self.assertRaises(Fault):
            self.custody(local_observed_at=124., adapter_pid=2, signature='wrong')
        self.assertEqual(before, self.records())

    def test_missing_legacy_observation_time_does_not_suppress_first_report(self):
        self.custody()
        def remove_time(tx):
            action = tx.need('actions', 'command')
            action['custody'].pop('observed_at')
            tx.put('actions', 'command', action)
        self.app.stores['site'].execute(remove_time)
        self.now += 1
        reply = self.custody()
        self.assertEqual(self.now, reply['observed_at'])
        self.assertEqual(self.now, self.records()[0]['custody']['observed_at'])

    def test_live_observations_still_refresh_command_watch(self):
        for state in ('launching', 'running'):
            first = self.custody(state, runner_pid=1)
            watch = self.records()[0]['watch_until']
            self.now += 5
            again = self.custody(state, runner_pid=1)
            self.assertGreater(again['observed_at'], first['observed_at'])
            self.assertGreater(self.records()[0]['watch_until'], watch)
            self.assertEqual(self.now, self.records()[1]['last_progress_at'])

    def test_starting_keeps_existing_observation_behavior(self):
        first = self.custody('starting')
        self.now += 1
        self.assertGreater(self.custody('starting')['observed_at'], first['observed_at'])


if __name__ == '__main__':
    unittest.main()
