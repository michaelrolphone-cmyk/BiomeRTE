"""Behavioral regression coverage for reservation-preserving redeployment."""
import time
import unittest
from unittest.mock import patch

from biomerte.app import Application
from biomerte.api import Context, command
from biomerte.store import Fault
from tests import test_runtime


class RedeploymentHold(unittest.TestCase):
    call = test_runtime.Runtime.call

    def setUp(self):
        test_runtime.Runtime.setUp(self)
        self.call('cap.define', {'name': 'deploy.test', 'inputs': {},
                                 'outputs': {'evidence': 'obj'}})
        self.advertise()

    def advertise(self, resource='port', provider='device', service='control',
                  capacity=1, state='ready', configuration=None):
        config = {'execution_custody': True,
                  'reservation_policy': {'idle_seconds': 10,
                                         'release_after_result': False}}
        config.update(configuration or {})
        return self.call('cap.offer', dict(
            provider=provider, contract='deploy.test:1', resource=resource,
            service=service, capacity=capacity, state=state,
            configuration=config))

    def acquire(self, task, resource='port', **extra):
        args = dict(task=task, contract='deploy.test:1', **extra)
        if resource is not None:
            args['resource'] = resource
        return self.call('cap.acquire', args)

    def lease(self, value):
        return self.call('cap.lease', {'lease': value['id']
                                      if isinstance(value, dict) else value})

    def fence(self, value):
        return self.call('cap.fenced', {
            'provider': value['provider'], 'lease': value['id'],
            'generation': value['generation']})

    def invoke(self, lease, operation):
        return self.call('cap.invoke', {
            'lease': lease['id'], 'operation': operation, 'inputs': {}})

    def claim(self, action):
        return self.call('provider.claim', {
            'provider': action['provider'], 'operation': action['id'],
            'generation': action['generation']})

    def settle(self, action):
        return self.call('provider.result', {
            'provider': action['provider'], 'operation': action['id'],
            'generation': action['generation'], 'state': 'succeeded',
            'result': {'evidence': {'quiescent': True}}})

    def begin(self, id='update', **selector):
        args = dict(id=id, reason='test firmware/service redeployment',
                    expected_version='v2')
        args.update(selector or {'resource': 'port'})
        return self.call('cap.redeploy.begin', args)

    def status(self, hold):
        return self.call('cap.redeploy.status', {'id': hold['id']})

    def complete(self, hold, resources=('port',), version='v2', **extra):
        args = dict(id=hold['id'], generation=hold['generation'],
                    evidence={'resources': {
                        resource: {'healthy': True, 'version': version}
                        for resource in resources}})
        args.update(extra)
        return self.call('cap.redeploy.complete', args)


    def retarget(self, hold, version='v3', reason='Repair failed candidate', rid=None):
        return self.call('cap.redeploy.retarget', {
            'id': hold['id'], 'generation': hold['generation'],
            'expected_version': version, 'reason': reason}, rid=rid)

    def test_retarget_preserves_streams_reservations_and_undispatched_work(self):
        self.a.config.update(stream_router_secret='s' * 32,
                             stream_gateway_url='wss://stream.example')
        self.advertise(configuration={
            'lease_class': 'persistent',
            'stream': {'enabled': True, 'lanes': ['data']},
            'reservation_policy': {'release_after_result': False}})
        first = self.fence(self.acquire('retarget-viewer-one'))
        second = self.acquire('retarget-viewer-two')
        stream = self.call('stream.open', {'lease': first['id']})
        action = self.invoke(first, 'retarget-pending')
        hold = self.begin()
        before = self.status(hold)
        self.assertIn(action['id'], before['held_operations'])
        self.assertIn(stream['id'], before['held_streams'])
        self.assertTrue(before['blockers'])
        records = [('leases', first['id']), ('leases', second['id']),
                   ('actions', action['id']), ('_stream_grants', stream['id']),
                   ('_redeploy_resources', 'port')]
        def preserved():
            return self.a.stores['site'].execute(
                lambda tx: [tx.get(table, key) for table, key in records],
                read_only=True)
        original_records = preserved()
        changed = self.retarget(hold)
        for key in ('id', 'owner', 'generation', 'selector', 'resources', 'state',
                    'held_leases', 'held_operations', 'held_streams', 'blockers'):
            self.assertEqual(changed[key], before[key], key)
        self.assertEqual(preserved(), original_records)
        self.assertFalse(changed['ready_to_deploy'])
        with self.assertRaises(Fault):
            self.complete(changed, version='v3')

    def test_retarget_does_not_settle_claimed_effect_or_clear_blocker(self):
        lease = self.fence(self.acquire('retarget-effect-owner'))
        action = self.claim(self.invoke(lease, 'retarget-claimed-effect'))
        hold = self.begin()
        before = self.status(hold)
        changed = self.retarget(hold)
        self.assertEqual(changed['blockers'], before['blockers'])
        self.assertTrue(any(row.get('operation') == action['id']
                            for row in changed['blockers']))
        self.assertFalse(changed['ready_to_deploy'])
        self.assertEqual(self.lease(lease)['generation'], lease['generation'])
        with self.assertRaises(Fault):
            self.complete(changed, version='v3')

    def test_retarget_keeps_failed_hold_failed_until_explicit_retry(self):
        lease = self.acquire('retarget-failed-owner')
        hold = self.begin()
        failed = self.call('cap.redeploy.fail', {
            'id': hold['id'], 'generation': hold['generation'],
            'reason': 'No rear video', 'evidence': {'rear_jpegs': 0}})
        changed = self.retarget(failed)
        self.assertEqual(changed['state'], 'failed')
        self.assertEqual(changed['failure'], failed['failure'])
        self.assertEqual(changed['generation'], failed['generation'])
        self.assertEqual(self.lease(lease)['state'], 'held')
        with self.assertRaises(Fault):
            self.call('cap.redeploy.submit', {
                'id': changed['id'], 'generation': changed['generation'],
                'contract': 'deploy.test:1', 'inputs': {}, 'operation': 'still-failed'})
        retried = self.call('cap.redeploy.retry', {
            'id': changed['id'], 'generation': changed['generation']})
        self.assertEqual(retried['expected_version'], 'v3')
        self.assertEqual(retried['version_history'], changed['version_history'])
        self.assertTrue(retried['ready_to_deploy'])
        self.assertEqual(self.lease(lease)['state'], 'held')

    def test_retarget_rejects_other_owner_scoped_actor_stale_and_blank_input(self):
        hold = self.begin()
        args = dict(id=hold['id'], generation=hold['generation'],
                    expected_version='v3', reason='Repair failed candidate')
        for ctx in (Context(self.a, actor='other-operator'),
                    Context(self.a, actor='admin',
                            permissions=['cap.redeploy.retarget'])):
            with self.subTest(actor=ctx.actor, permissions=ctx.permissions):
                with self.assertRaises(Fault):
                    self.a.stores['site'].execute(
                        lambda tx: command(tx, ctx, 'cap.redeploy.retarget',
                                           args, 'unauthorized-retarget'))
        with self.assertRaises(Fault):
            self.retarget(dict(hold, generation=hold['generation'] + 1))
        for version, reason in (('', 'reason'), ('  ', 'reason'), ('v3', ''),
                                ('v3', '  ')):
            with self.subTest(version=version, reason=reason), self.assertRaises(Fault):
                self.retarget(hold, version=version, reason=reason)
        self.assertEqual(self.status(hold), hold)

    def test_retarget_requires_new_version_healthy_complete_evidence(self):
        self.advertise(resource='second-port')
        first = self.acquire('retarget-first')
        second = self.acquire('retarget-second', resource='second-port')
        changed = self.retarget(self.begin(resources=['port', 'second-port']))
        with self.assertRaisesRegex(Fault, 'does not match'):
            self.complete(changed, resources=('port', 'second-port'), version='v2')
        for observations in (
                {},
                {'port': {'healthy': True, 'version': 'v3'}},
                {'port': {'healthy': True, 'version': 'v3'},
                 'second-port': {'healthy': False, 'version': 'v3'}},
                {'port': {'healthy': True, 'version': 'v3'},
                 'second-port': {'healthy': True, 'version': ''}}):
            with self.subTest(observations=observations), self.assertRaises(Fault):
                self.call('cap.redeploy.complete', {
                    'id': changed['id'], 'generation': changed['generation'],
                    'evidence': {'resources': observations}})
            self.assertEqual(self.lease(first)['state'], 'held')
            self.assertEqual(self.lease(second)['state'], 'held')
        completed = self.complete(changed, resources=('port', 'second-port'), version='v3')
        self.assertEqual(completed['state'], 'completed')
        self.assertEqual(completed['version_history'], changed['version_history'])
        with self.assertRaisesRegex(Fault, 'already completed'):
            self.retarget(completed, version='v4')
        self.assertEqual(self.status(completed), completed)

    def test_retarget_history_and_idempotence_survive_restart(self):
        hold = self.begin()
        changed = self.retarget(hold, rid='retarget-once')
        self.assertEqual(self.retarget(hold, rid='retarget-once'), changed)
        self.assertEqual(self.retarget(changed), changed)
        history = changed['version_history']
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['previous_version'], 'v2')
        self.assertEqual(history[0]['expected_version'], 'v3')
        self.assertEqual(history[0]['reason'], 'Repair failed candidate')
        self.assertEqual(history[0]['actor'], hold['owner'])
        self.assertEqual(history[0]['generation'], hold['generation'])
        with self.assertRaises(Fault):
            self.retarget(changed, version='v4', rid='retarget-once')
        newer = self.retarget(changed, version='v4', reason='Verified repair target')
        self.a = Application(self.home)
        restored = self.status(hold)
        self.assertEqual(restored, newer)
        self.assertEqual(restored['version_history'][0], history[0])
        self.assertEqual(restored['version_history'][1]['previous_version'], 'v3')
        self.assertEqual(restored['version_history'][1]['expected_version'], 'v4')
        with self.assertRaises(Fault):
            self.complete(restored, version='v3')
        self.complete(restored, version='v4')

    def test_idle_and_waiting_reservations_keep_identity_and_fifo(self):
        first = self.fence(self.acquire('first'))
        second = self.acquire('second')
        third = self.acquire('third')
        self.assertEqual(second['state'], 'queued')
        hold = self.begin()
        self.assertTrue(hold['ready_to_deploy'])
        for original in (first, second, third):
            held = self.lease(original)
            self.assertEqual(held['id'], original['id'])
            self.assertEqual(held['state'], 'held')
            self.assertEqual(held['owner'], original['owner'])
        self.assertEqual(self.acquire('first')['id'], first['id'])
        self.complete(hold)
        resumed = self.lease(first)
        self.assertEqual(resumed['state'], 'fencing')
        self.assertGreater(resumed['generation'], first['generation'])
        self.assertEqual(self.lease(second)['state'], 'queued')
        self.call('cap.release', {'lease': first['id']})
        self.assertEqual(self.lease(second)['state'], 'fencing')
        self.assertEqual(self.lease(third)['state'], 'queued')
        self.call('cap.release', {'lease': second['id']})
        self.assertEqual(self.lease(third)['state'], 'fencing')

    def test_fencing_and_unclaimed_command_are_held_then_refenced(self):
        lease = self.acquire('command-owner')
        action = self.invoke(lease, 'pending-command')
        self.assertEqual(action['state'], 'queued')
        hold = self.begin()
        self.assertTrue(hold['ready_to_deploy'])
        self.assertEqual(self.lease(lease)['state'], 'held')
        held_action = self.call('cap.status', {'operation': action['id']})
        self.assertEqual(held_action['state'], 'held')
        self.assertEqual(held_action['lease'], lease['id'])
        inbox = self.call('provider.poll', {'provider': 'device'})
        self.assertEqual(inbox['actions'], [])
        self.assertEqual(inbox['fences'], [])
        with self.assertRaises(Fault):
            self.fence(lease)
        with self.assertRaises(Fault):
            self.claim(action)
        self.complete(hold)
        resumed = self.lease(lease)
        self.assertEqual(resumed['id'], lease['id'])
        self.assertGreater(resumed['generation'], lease['generation'])
        self.fence(resumed)
        queued = self.call('cap.status', {'operation': action['id']})
        self.assertEqual(queued['id'], action['id'])
        self.assertEqual(queued['generation'], resumed['generation'])
        self.assertEqual(queued['state'], 'queued')
        with self.assertRaises(Fault):
            self.claim(action)
        self.claim(queued)
        self.settle(queued)
        self.assertEqual(self.call('cap.status', {
            'operation': action['id']})['state'], 'succeeded')

    def test_new_pinned_work_waits_while_other_resource_runs(self):
        self.advertise(resource='other', provider='other-device')
        hold = self.begin()
        queued = self.acquire('new-pinned')
        self.assertEqual(queued['state'], 'held')
        automatic = self.acquire('automatic', resource=None)
        self.assertEqual(automatic['resource'], 'other')
        self.assertEqual(automatic['state'], 'fencing')
        self.fence(automatic)
        action = self.invoke(automatic, 'other-action')
        self.claim(action)
        self.settle(action)
        self.assertEqual(self.lease(queued)['state'], 'held')
        self.complete(hold)
        self.assertEqual(self.lease(queued)['state'], 'fencing')

    def test_combined_submission_is_retained_during_hold(self):
        hold = self.begin()
        submission = self.call('cap.submit', {
            'contract': 'deploy.test:1', 'resource': 'port',
            'operation': 'held-submission', 'inputs': {}})
        self.assertEqual(submission['state'], 'held')
        self.a = Application(self.home)
        again = self.call('cap.submit', {
            'contract': 'deploy.test:1', 'resource': 'port',
            'operation': 'held-submission', 'inputs': {}})
        self.assertEqual(again['lease'], submission['lease'])
        self.assertEqual(again['state'], 'held')
        self.complete(hold)
        lease = self.lease(submission['lease'])
        self.fence(lease)
        action = self.call('cap.status', {'operation': 'held-submission'})
        self.claim(action)
        self.settle(action)
        self.assertEqual(self.call('cap.submission', {
            'operation': 'held-submission'})['state'], 'succeeded')
        self.assertEqual(self.lease(lease)['state'], 'released')

    def test_claimed_effect_drains_without_invalidating_its_completion(self):
        lease = self.fence(self.acquire('running'))
        action = self.invoke(lease, 'running-command')
        self.claim(action)
        hold = self.begin()
        self.assertEqual(hold['state'], 'draining')
        self.assertFalse(hold['ready_to_deploy'])
        self.assertTrue(hold['blockers'])
        self.assertEqual(self.lease(lease)['generation'], lease['generation'])
        self.assertEqual(self.acquire('after-running')['state'], 'held')
        with self.assertRaises(Fault):
            self.complete(hold)
        self.call('cap.heartbeat', {
            'provider': action['provider'], 'operation': action['id'],
            'generation': action['generation']})
        self.settle(action)
        settled_hold = self.status(hold)
        self.assertTrue(settled_hold['ready_to_deploy'])
        self.assertEqual(settled_hold['blockers'], [])
        self.assertEqual(self.lease(lease)['state'], 'held')
        self.complete(hold)
        self.assertGreater(self.lease(lease)['generation'], lease['generation'])

    def test_uncertain_effect_and_requested_stop_are_real_blockers(self):
        lease = self.fence(self.acquire('uncertain'))
        action = self.invoke(lease, 'uncertain-command')
        self.claim(action)
        self.call('provider.result', {
            'provider': action['provider'], 'operation': action['id'],
            'generation': action['generation'], 'state': 'uncertain',
            'result': {'reason': 'connection lost'}})
        hold = self.begin()
        self.call('cap.kill', {'operation': action['id']})
        self.assertFalse(self.status(hold)['ready_to_deploy'])
        with self.assertRaises(Fault):
            self.complete(hold)
        self.settle(action)
        self.assertTrue(self.status(hold)['ready_to_deploy'])

    def test_long_hold_survives_restart_offline_and_expiry_sweep(self):
        lease = self.acquire('persistent-wait')
        hold = self.begin()
        self.advertise(state='offline')
        future = time.time() + 86400
        with patch('biomerte.lifecycle.time.time', return_value=future):
            self.a.tick('site')
            self.assertEqual(self.lease(lease)['state'], 'held')
        self.a = Application(self.home)
        restored = self.status(hold)
        self.assertEqual(restored['generation'], hold['generation'])
        self.assertEqual(self.lease(lease)['id'], lease['id'])
        self.assertEqual(self.lease(lease)['state'], 'held')
        self.advertise(state='ready')
        self.complete(hold)
        self.assertEqual(self.lease(lease)['state'], 'fencing')

    def test_failure_and_wrong_version_leave_reservations_held(self):
        lease = self.acquire('wait-through-failure')
        hold = self.begin()
        with self.assertRaises(Fault):
            self.complete(hold, version='v1')
        with self.assertRaises(Fault):
            self.call('cap.redeploy.complete', {
                'id': hold['id'], 'generation': hold['generation'],
                'evidence': {}})
        failed = self.call('cap.redeploy.fail', {
            'id': hold['id'], 'generation': hold['generation'],
            'reason': 'health probe failed'})
        self.assertEqual(self.lease(lease)['state'], 'held')
        self.a.tick('site')
        self.assertEqual(self.lease(lease)['state'], 'held')
        retry = self.call('cap.redeploy.retry', {
            'id': failed['id'], 'generation': failed['generation']})
        self.assertEqual(retry['id'], hold['id'])
        self.complete(retry)
        self.assertEqual(self.lease(lease)['state'], 'fencing')

    def test_unhealthy_or_incomplete_multi_resource_evidence_cannot_thaw(self):
        self.advertise(resource='second-port')
        first = self.acquire('first')
        second = self.acquire('second', resource='second-port')
        hold = self.begin(resources=['port', 'second-port'])
        with self.assertRaises(Fault):
            self.complete(hold, resources=('port',))
        with self.assertRaises(Fault):
            self.call('cap.redeploy.complete', {
                'id': hold['id'], 'generation': hold['generation'],
                'evidence': {'resources': {
                    'port': {'healthy': True, 'version': 'v2'},
                    'second-port': {'healthy': False, 'version': 'v2'}}}})
        self.assertEqual(self.lease(first)['state'], 'held')
        self.assertEqual(self.lease(second)['state'], 'held')
        self.complete(hold, resources=('port', 'second-port'))
        self.assertEqual(self.lease(first)['state'], 'fencing')
        self.assertEqual(self.lease(second)['state'], 'fencing')

    def test_cancelled_reservation_is_never_resurrected(self):
        first = self.acquire('first')
        cancelled = self.acquire('cancelled')
        hold = self.begin()
        self.call('cap.release', {'lease': cancelled['id']})
        self.assertEqual(self.lease(cancelled)['state'], 'released')
        self.complete(hold)
        self.call('cap.release', {'lease': first['id']})
        self.assertEqual(self.lease(cancelled)['state'], 'released')
        self.assertEqual(self.call('provider.poll', {
            'provider': 'device'})['fences'], [])

    def test_kill_of_held_command_is_terminal_and_does_not_resume(self):
        lease = self.fence(self.acquire('cancel-command'))
        action = self.invoke(lease, 'cancel-held')
        hold = self.begin()
        self.call('cap.kill', {'operation': action['id']})
        self.complete(hold)
        self.assertIn(self.call('cap.status', {
            'operation': action['id']})['state'], ('abandoned', 'failed'))
        self.assertFalse(any(a['id'] == action['id']
                             for a in self.call('provider.poll', {
                                 'provider': 'device'})['actions']))

    def test_same_begin_and_completion_are_idempotent_and_old_finish_is_fenced(self):
        self.acquire('one')
        first = self.begin(id='update-one')
        repeated = self.begin(id='update-one')
        self.assertEqual(repeated['id'], first['id'])
        self.assertEqual(repeated['generation'], first['generation'])
        with self.assertRaises(Fault):
            self.begin(id='competing-update')
        finished = self.complete(first)
        self.assertEqual(self.complete(first)['state'], finished['state'])
        second = self.begin(id='update-two')
        waiting = self.acquire('two')
        self.complete(first)
        self.assertEqual(self.lease(waiting)['state'], 'held')
        with self.assertRaises(Fault):
            self.complete(dict(second, generation=second['generation'] + 1))
        self.assertEqual(self.lease(waiting)['state'], 'held')
        self.complete(second)

    def test_provider_and_service_selectors_hold_every_matching_resource(self):
        self.advertise(resource='second-port', service='control')
        self.advertise(resource='unrelated', provider='elsewhere', service='other')
        one = self.acquire('one')
        two = self.acquire('two', resource='second-port')
        other = self.acquire('other', resource='unrelated')
        hold = self.begin(provider='device')
        self.assertEqual(self.lease(one)['state'], 'held')
        self.assertEqual(self.lease(two)['state'], 'held')
        self.assertEqual(self.lease(other)['state'], 'fencing')
        self.complete(hold, resources=('port', 'second-port'))
        service = self.begin(id='service-update', service='control')
        self.assertEqual(self.lease(one)['state'], 'held')
        self.assertEqual(self.lease(two)['state'], 'held')
        self.assertEqual(self.lease(other)['state'], 'fencing')
        self.complete(service, resources=('port', 'second-port'))

    def test_redeploy_is_reported_by_resource_status(self):
        lease = self.acquire('visible')
        hold = self.begin()
        status = self.call('cap.resource.status', {'resource': 'port'})
        self.assertNotEqual(status['state'], 'free')
        self.assertIn(hold['id'], str(status))
        self.assertEqual(self.lease(lease)['state'], 'held')

    def test_shared_stream_subscriptions_survive_and_old_stream_authority_does_not(self):
        self.a.config.update(stream_router_secret='s' * 32,
                             stream_gateway_url='wss://stream.example')
        self.advertise(configuration={
            'lease_class': 'persistent',
            'stream': {'enabled': True, 'lanes': ['data']},
            'reservation_policy': {'release_after_result': False}})
        first = self.fence(self.acquire('viewer-one'))
        second = self.acquire('viewer-two')
        original = self.call('stream.open', {'lease': first['id']})
        hold = self.begin()
        self.assertEqual(self.lease(first)['state'], 'held')
        self.assertEqual(self.lease(second)['state'], 'held')
        with self.assertRaises(Fault):
            self.call('stream.attach', {
                'session': original['id'], 'role': 'server',
                'lease': second['id']})
        self.a.tick('site')
        self.assertEqual(self.lease(second)['state'], 'held')
        self.assertFalse(self.status(hold)['ready_to_deploy'])
        with self.assertRaises(Fault):
            self.complete(hold)
        self.call('cap.redeploy.quiesce', {
            'id': hold['id'], 'generation': hold['generation'],
            'evidence': {'streams': {original['id']: {
                'quiescent': True, 'method': 'provider_stopped',
                'observation': {'service': 'fixture', 'state': 'stopped'}}}}})
        self.complete(hold)
        for lease in (self.lease(first), self.lease(second)):
            if lease['state'] == 'fencing':
                self.fence(lease)
        fresh = self.call('stream.open', {'lease': first['id']})
        shared = self.call('stream.open', {'lease': second['id']})
        self.assertEqual(fresh['id'], shared['id'])
        self.assertGreater(fresh['generation'], original['generation'])
        self.assertEqual(self.lease(first)['id'], first['id'])
        self.assertEqual(self.lease(second)['id'], second['id'])

    def test_only_current_deployment_maintenance_can_use_held_resource(self):
        ordinary = self.acquire('ordinary-owner')
        hold = self.begin()
        waiting = self.call('cap.submit', {
            'contract': 'deploy.test:1', 'resource': 'port',
            'operation': 'ordinary-waiting', 'inputs': {}})
        self.assertEqual(waiting['state'], 'held')
        with self.assertRaises(Fault):
            self.call('cap.redeploy.submit', {
                'id': hold['id'], 'generation': hold['generation'] + 1,
                'contract': 'deploy.test:1', 'resource': 'port',
                'inputs': {}, 'operation': 'wrong-deployment'})
        maintenance = self.call('cap.redeploy.submit', {
            'id': hold['id'], 'generation': hold['generation'],
            'contract': 'deploy.test:1', 'resource': 'port',
            'inputs': {}, 'operation': 'maintenance-update'})
        maintenance_lease = self.lease(maintenance['lease'])
        self.assertEqual(maintenance_lease['state'], 'fencing')
        self.fence(maintenance_lease)
        action = self.call('cap.status', {'operation': 'maintenance-update'})
        inbox = self.call('provider.poll', {'provider': 'device'})
        self.assertEqual([a['id'] for a in inbox['actions']], [action['id']])
        self.claim(action)
        with self.assertRaises(Fault):
            self.complete(hold)
        self.assertEqual(self.lease(ordinary)['state'], 'held')
        self.settle(action)
        self.complete(hold)
        self.assertEqual(self.lease(ordinary)['state'], 'fencing')

    def test_bidirectional_stream_waits_for_current_quiescence_evidence(self):
        self.a.config.update(stream_router_secret='s' * 32,
                             stream_gateway_url='wss://stream.example')
        self.advertise(configuration={
            'lease_class': 'persistent',
            'stream': {'enabled': True, 'direction': 'bidirectional'},
            'reservation_policy': {'release_after_result': False}})
        lease = self.fence(self.acquire('control-session'))
        stream = self.call('stream.open', {'lease': lease['id']})
        hold = self.begin()
        self.assertFalse(hold['ready_to_deploy'])
        with self.assertRaises(Fault):
            self.complete(hold)
        with self.assertRaises(Fault):
            self.call('cap.redeploy.quiesce', {
                'id': hold['id'], 'generation': hold['generation'],
                'evidence': {'streams': {stream['id']: {
                    'quiescent': False, 'method': 'provider_stopped',
                    'observation': {'provider': 'device'}}}}})
        quiescent = self.call('cap.redeploy.quiesce', {
            'id': hold['id'], 'generation': hold['generation'],
            'evidence': {'streams': {stream['id']: {
                'quiescent': True, 'method': 'provider_stopped',
                'observation': {'provider': 'device', 'state': 'stopped'}}}}})
        self.assertTrue(quiescent['ready_to_deploy'])
        self.complete(hold)
        self.assertEqual(self.lease(lease)['state'], 'fencing')


    def test_workflow_waits_through_held_lease_and_held_command(self):
        graph = {'inputs': {}, 'outputs': {}, 'start': 'run', 'nodes': {
            'run': {'type': 'service', 'contract': 'deploy.test:1',
                    'resource': 'port', 'args': {}, 'lease_scope': 'activity',
                    'next': 'done'},
            'done': {'type': 'end', 'result': {}}}}
        self.call('process.publish', {'name': 'during-update', 'graph': graph})
        first = self.begin(id='before-workflow')
        run = self.call('process.start', {'name': 'during-update', 'inputs': {}})
        test_runtime.Runtime.drain(self)
        self.assertEqual(self.call('process.get', {'run': run['id']})['state'],
                         'running')
        self.complete(first)
        test_runtime.Runtime.drain(self)
        pending = self.call('provider.poll', {'provider': 'device'})
        self.assertTrue(pending['fences'])
        second = self.begin(id='before-command')
        test_runtime.Runtime.drain(self)
        self.assertEqual(self.call('process.get', {'run': run['id']})['state'],
                         'running')
        self.complete(second)
        for lease in self.call('provider.poll', {'provider': 'device'})['fences']:
            self.fence(lease)
        test_runtime.Runtime.drain(self)
        action, = self.call('provider.poll', {'provider': 'device'})['actions']
        self.claim(action)
        self.settle(action)
        test_runtime.Runtime.drain(self)
        self.assertEqual(self.call('process.get', {'run': run['id']})['state'],
                         'completed')

    def test_mcp_guidance_keeps_held_lease_live(self):
        from biomerte.mcp import caller_view
        lease = self.acquire('mcp-caller')
        self.begin()
        view = caller_view('cap.lease', self.lease(lease), 'site')
        self.assertEqual(view['state'], 'held')
        self.assertEqual(view['next_call']['operation'], 'cap.lease')
        self.assertNotIn('released', view['next_action'].lower())


    def cancel_held_workflow(self, scoped):
        service = {'type': 'service', 'contract': 'deploy.test:1',
                   'resource': 'port', 'args': {}, 'next': 'done'}
        if scoped:
            service['lease_scope'] = 'activity'
        graph = {'inputs': {}, 'outputs': {}, 'start': 'run',
                 'nodes': {'run': service,
                           'done': {'type': 'end', 'result': {}}}}
        self.call('process.publish', {'name': 'cancel-held', 'graph': graph})
        run = self.call('process.start', {'name': 'cancel-held', 'inputs': {}})
        test_runtime.Runtime.drain(self)
        running = self.call('process.get', {'run': run['id']})
        action_id = running['tokens'][0]['action']
        action = self.call('cap.status', {'operation': action_id})
        hold = self.begin()
        self.assertEqual(self.call('cap.status', {
            'operation': action_id})['state'], 'held')
        self.call('process.cancel', {'run': run['id']})
        self.assertEqual(self.call('process.get', {
            'run': run['id']})['state'], 'cancelled')
        self.assertIn(self.call('cap.status', {
            'operation': action_id})['state'], ('failed', 'abandoned'))
        self.assertEqual(self.lease(action['lease'])['state'], 'released')
        self.complete(hold)
        inbox = self.call('provider.poll', {'provider': 'device'})
        self.assertEqual(inbox['actions'], [])
        self.assertEqual(inbox['fences'], [])

    def test_cancelled_workflow_discards_held_activity_command(self):
        self.cancel_held_workflow(scoped=True)

    def test_cancelled_workflow_discards_held_task_command(self):
        self.cancel_held_workflow(scoped=False)



if __name__ == '__main__':
    unittest.main()
