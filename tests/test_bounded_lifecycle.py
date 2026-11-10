"""Regression coverage for bounded lease and result lifecycle."""
import tempfile
import unittest
from unittest.mock import patch
from biomerte.app import Application, initialize
from biomerte.store import Fault, uid


class BoundedLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        initialize(self.tmp.name)
        self.app = Application(self.tmp.name)
        self.now = 10000.
        self.clock = patch('time.time', side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.call('cap.define', name='sample', inputs={}, outputs={})

    def call(self, op, **args):
        return self.app.call(op, args, uid())

    def offer(self, cls='command', **cfg):
        cfg['lease_class'] = cls
        return self.call('cap.offer', provider='test', contract='sample:1', resource='slot',
                         configuration=cfg)

    def acquire(self, task='one'):
        return self.call('cap.acquire', task=task, contract='sample:1', resource='slot')

    def action(self, cls='command', **cfg):
        self.offer(cls, **cfg)
        lease = self.acquire()
        self.call('cap.fenced', provider='test', lease=lease['id'], generation=lease['generation'])
        action = self.call('cap.invoke', lease=lease['id'], inputs={}, operation='one')
        self.call('provider.claim', provider='test', operation='one', generation=lease['generation'])
        return lease, action

    def test_fence_timeout_abandons_queued_action_and_promotes_without_caller(self):
        self.offer('persistent')
        lease = self.acquire()
        self.call('cap.invoke', lease=lease['id'], inputs={}, operation='one')
        second = self.acquire('two')
        self.now += 121
        self.app.tick('site')
        self.assertEqual('released', self.call('cap.lease', lease=lease['id'])['state'])
        self.assertEqual('abandoned', self.call('cap.status', operation='one')['state'])
        self.assertEqual('fencing', self.call('cap.lease', lease=second['id'])['state'])
        with self.assertRaises(Fault) as e:
            self.call('cap.fenced', provider='test', lease=lease['id'], generation=lease['generation'])
        self.assertEqual(409, e.exception.status)

    def test_watch_progress_and_restart(self):
        lease, action = self.action()
        self.now += 100
        self.call('cap.heartbeat', provider='test', operation='one', generation=lease['generation'])
        self.app = Application(self.tmp.name)
        self.now += 100
        self.app.tick('site')
        self.assertEqual('claimed', self.call('cap.status', operation='one')['state'])
        self.now += 21
        self.app.tick('site')
        self.assertEqual('abandoned', self.call('cap.status', operation='one')['state'])
        for op in ('provider.result', 'provider.claim', 'cap.heartbeat'):
            args = dict(provider='test', operation='one', generation=action['generation'])
            if op == 'provider.result':
                args.update(state='succeeded', result={})
            with self.assertRaises(Fault) as e:
                self.call(op, **args)
            self.assertEqual(409, e.exception.status)

    def test_persistent_has_no_watch_or_heartbeat_requirement(self):
        lease, action = self.action('persistent')
        self.now += 100000
        self.app.tick('site')
        self.assertEqual('claimed', self.call('cap.status', operation='one')['state'])
        self.call('provider.result', provider='test', operation='one',
                  generation=action['generation'], state='succeeded', result={})
        self.assertEqual('bound', self.call('cap.lease', lease=lease['id'])['state'])

    def test_long_command_misses_are_elapsed_intervals_not_tick_count(self):
        lease, action = self.action('long_command', heartbeat_interval_seconds=10, missed_heartbeats_limit=3)
        for i in range(20):
            self.app.tick('site')
        self.now += 29
        self.app.tick('site')
        self.assertEqual('claimed', self.call('cap.status', operation='one')['state'])
        self.call('cap.heartbeat', provider='test', operation='one', generation=lease['generation'])
        self.now += 30
        self.app.tick('site')
        self.assertEqual('abandoned', self.call('cap.status', operation='one')['state'])

    def test_destination_ack_is_not_redelivered_and_expiry_cleans_records(self):
        self.call('destination.register', id='bot', kind='app', inbox='results',
                  ttl_seconds=3600, retention_seconds=1800)
        self.offer()
        lease = self.acquire()
        self.call('cap.fenced', provider='test', lease=lease['id'], generation=lease['generation'])
        a = self.call('cap.invoke', lease=lease['id'], inputs={}, operation='one', destination='bot')
        self.call('provider.claim', provider='test', operation='one', generation=a['generation'])
        self.call('provider.result', provider='test', operation='one', generation=a['generation'],
                  state='succeeded', result={})
        self.assertEqual(1, len(self.call('inbox.list', destination='bot')))
        self.call('inbox.ack', destination='bot', operation='one')
        self.app.tick('site')
        self.assertEqual([], self.call('inbox.list', destination='bot'))
        self.now += 901
        self.app.tick('site')
        with self.assertRaises(Fault):
            self.call('cap.status', operation='one')
        # Generation remains monotonic after action, lease and guard collection.
        again = self.acquire('two')
        self.assertGreater(again['generation'], a['generation'])

    def test_no_destination_without_registration_and_no_ephemeral_inbox(self):
        self.offer()
        lease = self.acquire()
        with self.assertRaises(Fault):
            self.call('cap.invoke', lease=lease['id'], inputs={}, operation='one', destination='absent')
        a = self.call('cap.invoke', lease=lease['id'], inputs={}, operation='two')
        self.assertEqual('', a['destination'])
        with self.app.stores['site'].connect() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM records WHERE kind='result_inbox'").fetchone()[0])

    def test_invalid_policy_does_not_publish(self):
        for cfg in ({'lease_class': 'wrong'}, {'watch_ttl_seconds': 901},
                    {'heartbeat_interval_seconds': 0}, {'missed_heartbeats_limit': 1.2}):
            with self.assertRaises(Fault):
                self.call('cap.offer', provider='test', contract='sample:1', resource='slot', configuration=cfg)

    def test_recovery_uses_active_state_and_expiry_indexes_not_historical_bodies(self):
        from biomerte.store import Records
        from biomerte.api import Context
        from biomerte.providers import recover_reservations
        lease, action = self.action()
        def seed(tx):
            for i in range(25000):
                tx.put('actions','history-%05d'%i,dict(id='history-%05d'%i,lease='old',
                    generation=1,state='succeeded',result={'large_history':'historical'},expires_at=20000))
        self.app.stores['site'].execute(seed)
        original = Records.rows
        def no_history(tx, kind):
            if kind in ('actions','leases','guards','events','runs','artifacts','_receipt'):
                raise AssertionError('Recovery decoded complete historical '+kind)
            return original(tx,kind)
        self.now += 121
        with patch.object(Records,'rows',no_history):
            recovered = self.app.stores['site'].execute(lambda tx:recover_reservations(tx,Context(self.app)))
        self.assertEqual(1,recovered)
        self.assertEqual('abandoned',self.call('cap.status',operation='one')['state'])

    def test_derived_index_does_not_constrain_arbitrary_document_shapes(self):
        value={'state':{'mode':'ready'},'lease':{'nested':True},'expires_at':{'not_a_lifecycle':True}}
        self.app.stores['site'].execute(lambda tx:tx.put('custom','shape',value))
        result=self.app.stores['site'].execute(lambda tx:tx.need('custom','shape'))
        self.assertEqual(value,result)

    def test_adapter_launch_failure_records_nonexecution_without_masking_error(self):
        import os
        from biomerte.worker import Worker
        lease, action = self.action()
        class Remote:
            node='test'
            site='site'
            realm='site'
            def call(inner, name, args, rid=None):
                return self.app.call(name,args,rid or uid())
        worker=Worker(os.path.join(self.tmp.name,'worker.sqlite'),Remote(),
                      [{'contract':'sample:1','resource':'slot','argv':['/nonexistent/biomerte-adapter']}])
        try:
            worker.perform(action)
            result=self.call('cap.status',operation='one')
            self.assertEqual('failed',result['state'])
            self.assertFalse(result['result']['evidence']['executed'])
            self.assertTrue(result['result']['evidence']['quiescent'])
            self.assertIn('No such file',result['result']['reason'])
        finally:
            worker.db.close()

    def test_persistent_provider_offline_revokes_authority_without_duration_watch(self):
        lease,action=self.action('persistent')
        self.call('cap.offer',provider='test',contract='sample:1',resource='slot',state='offline',
                  configuration={'lease_class':'persistent'})
        self.app.tick('site')
        self.assertEqual('abandoned',self.call('cap.status',operation='one')['state'])
        self.assertEqual('provider_offline',self.call('cap.status',operation='one')['result']['reason'])
