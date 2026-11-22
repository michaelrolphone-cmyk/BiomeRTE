import unittest

from biomerte.api import Context, invoke
from biomerte.store import Fault
from tests import test_runtime as runtime


class ConditionalPublication(unittest.TestCase):
    call = runtime.Runtime.call

    def setUp(self):
        runtime.Runtime.setUp(self)
        self.scope = dict(provider='camera', resource='vision', service='vision-service')
        self.defs = {name: dict(name=name, inputs={}, outputs={}) for name in ('one', 'two')}
        self.rows = {}
        for name, definition in self.defs.items():
            self.call('cap.define', definition)
            self.rows[name] = self.call('cap.offer', dict(self.scope, contract=name + ':1',
                configuration={'custom': True}, capacity=3))

    def entry(self, name, reg, **offer):
        return dict(definition=self.defs[name], offer=dict(contract=name+':1', **offer),
                    expected_registration=reg)

    def inspect(self):
        return self.call('cap.registrations', dict(self.scope, contracts=['one:1', 'two:1']))['registrations']

    def test_stale_replay_warm_noop_and_offline_restore_with_custody(self):
        one, two = (self.rows[n]['registration'] for n in ('one', 'two'))
        stop = dict(self.scope, registrations={'one:1': one})
        self.call('cap.withdraw', stop, 'stop')
        offline, warm = self.inspect()
        self.assertEqual(offline['withdrawn_from'], one)
        self.assertNotEqual(offline['registration'], one)
        self.assertEqual(warm['registration'], two)
        args = dict(self.scope, publication_id='P', offers=[self.entry('one', one), self.entry('two', two)])
        self.assertEqual([r['status'] for r in self.call('cap.publish', args)['offers']], ['superseded','unchanged'])
        args['offers'][0]['expected_registration'] = offline['registration']
        result = self.call('cap.publish', args, 'restart')
        self.assertEqual([r['status'] for r in result['offers']], ['published','unchanged'])
        self.assertEqual(self.call('cap.publish', args, 'restart'), result)
        first, second = self.inspect()
        self.assertEqual(first['published_by'], 'P')
        self.assertNotIn('published_by', second)
        self.assertNotIn('withdrawn_from', first)
        self.assertEqual(second['registration'], two)
        self.assertEqual(self.call('cap.withdraw', stop)['superseded'], ['one:1'])
        self.assertEqual(self.call('cap.publish', args)['offers'][0]['status'], 'superseded')
        self.assertTrue(all(v['capacity']==3 and v['configuration']['custom']
                            for v in self.call('cap.discover')['offers'] if v['provider']=='camera'))

    def test_mismatched_definition_and_config_are_per_entry_and_cas_create_is_exclusive(self):
        one, two = (self.rows[n]['registration'] for n in ('one', 'two'))
        entries = [self.entry('one', one), self.entry('two', two, capacity=4)]
        entries[0]['definition'] = dict(self.defs['one'], description='different schema')
        missing = dict(definition=dict(name='three',inputs={},outputs={}),
                       offer=dict(contract='three:1'),expected_registration=None)
        reply = self.call('cap.publish', dict(self.scope, offers=[*entries, missing]))
        self.assertEqual([r['status'] for r in reply['offers']], ['invalid','different','missing'])
        self.assertEqual([r['registration'] for r in self.inspect()], [one,two])
        with self.assertRaises(Fault):
            self.call('cap.offer', dict(self.scope, contract='one:1', expected_absent=True, capacity=5))
        args = dict(self.scope, contract='one:1', expected_registration=one, capacity=4)
        revision = self.call('cap.offer', args)['registration']
        with self.assertRaises(Fault):
            self.call('cap.offer', args)
        self.assertEqual(self.inspect()[0]['registration'], revision)

    def test_second_stop_fences_an_unknown_publish_while_already_offline(self):
        self.call('cap.withdraw',dict(self.scope,registrations={'one:1':self.rows['one']['registration']}))
        offline=self.inspect()[0]['registration']
        pending=dict(self.scope,offers=[self.entry('one',offline)],publication_id='uncertain')
        stop=dict(self.scope,registrations={'one:1':offline})
        self.call('cap.withdraw',stop,'final-stop')
        self.assertEqual(self.call('cap.withdraw',stop,'final-stop')['withdrawn'],['one:1'])
        self.assertEqual(self.call('cap.publish',pending)['offers'][0]['status'],'superseded')
        latest=self.inspect()[0]
        self.assertEqual(latest['state'],'offline')
        self.assertEqual(latest['withdrawn_from'],offline)
        self.assertNotEqual(latest['registration'],offline)

    def test_restore_preserves_bound_lease_guard_capacity_and_other_providers(self):
        db=self.a.stores['site']
        other=self.call('cap.offer',dict(provider='another',resource='different',service='other',contract='one:1'))
        lease=self.call('cap.acquire',dict(contract='one:1',resource='vision',task='still-running'))
        self.call('cap.fenced',dict(provider='camera',lease=lease['id'],generation=lease['generation']))
        def state(tx):
            return [tx.need('leases',lease['id']),list(tx.rows('guards')),tx.need('_capacity','vision'),
                    tx.need('offers',other['id']),tx.need('offers',self.rows['one']['id'])['configuration']]
        original=db.execute(state,read_only=True)
        self.call('cap.withdraw',dict(self.scope,registrations={'one:1':self.rows['one']['registration']}))
        offline=self.inspect()[0]['registration']
        self.call('cap.publish',dict(self.scope, offers=[self.entry('one',offline)]))
        self.assertEqual(db.execute(state,read_only=True),original)

    def test_missing_definition_repair_requires_existing_enrollment_permission(self):
        db=self.a.stores['site']
        db.execute(lambda tx:tx.drop('contracts','one:1'))
        args=dict(self.scope, offers=[self.entry('one',self.rows['one']['registration'])])
        permissions={'cap.offer','offer:one:1','resource:vision'}
        def attempt(tx):
            return invoke(tx,Context(self.a,'camera','site',permissions),'cap.publish',args)
        with self.assertRaises(Fault) as error:
            db.execute(attempt)
        self.assertEqual(error.exception.status,403)
        permissions.add('cap.define')
        self.assertEqual(db.execute(attempt)['offers'][0]['status'],'published')
        self.assertEqual(db.execute(lambda tx:tx.need('contracts','one:1')['name'],read_only=True),'one')

    def test_absent_scalar_and_legacy_null_scope_cas(self):
        db=self.a.stores['site']
        def legacy(tx):
            row=tx.need('offers', self.rows['one']['id'])
            row.pop('registration')
            tx.put('offers',row['id'],row)
        db.execute(legacy)
        with self.assertRaises(Fault):
            self.call('cap.offer',dict(self.scope,service='other',contract='one:1',expected_registration=''))
        updated=self.call('cap.offer',dict(self.scope,contract='one:1',expected_registration='',publication_id='scalar-CAS'))
        self.assertEqual(self.inspect()[0]['published_by'],'scalar-CAS')
        with self.assertRaises(Fault):
            self.call('cap.offer',dict(self.scope,contract='one:1',expected_registration=''))
        self.call('cap.define',dict(name='three',inputs={},outputs={}))
        args=dict(self.scope,contract='three:1',expected_absent=True)
        self.call('cap.offer',args)
        with self.assertRaises(Fault):
            self.call('cap.offer',args)
        self.assertEqual(self.inspect()[0]['registration'],updated['registration'])

    def test_whole_page_enforces_enrolled_scope_and_service_identity(self):
        db = self.a.stores['site']
        args = dict(self.scope, offers=[self.entry(n,self.rows[n]['registration']) for n in self.defs])
        for name, arguments in [('cap.registrations',dict(self.scope,contracts=['one:1','two:1'])),
                                ('cap.publish',args)]:
            for actor, perms in [('camera',{'cap.define','cap.offer','resource:vision','offer:one:1'}),
                                 ('other',{'cap.define','cap.offer','resource:vision','offer:one:1','offer:two:1'})]:
                with self.assertRaises(Fault) as exc:
                    db.execute(lambda tx:invoke(tx,Context(self.a,actor,'site',perms),name,arguments))
                self.assertEqual(exc.exception.status,403)
        with self.assertRaises(Fault):
            self.call('cap.registrations',dict(self.scope,contracts=['one:1']*17))
        rival = self.call('cap.registrations',dict(self.scope,service='other',contracts=['one:1']))
        self.assertEqual(rival['registrations'],[dict(contract='one:1',status='superseded',state=None,registration=None)])
