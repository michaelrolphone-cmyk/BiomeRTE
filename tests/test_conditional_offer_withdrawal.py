import unittest

from biomerte.api import Context, invoke
from biomerte.store import Fault
from tests import test_runtime


class ConditionalWithdrawal(unittest.TestCase):
    setUp = test_runtime.Runtime.setUp
    call = test_runtime.Runtime.call

    def test_delayed_or_replayed_shutdown_preserves_successors_and_other_offers(self):
        for name in ('one', 'two', 'three'):
            self.call('cap.define', {'name':name, 'inputs':{}, 'outputs':{}})
        args = dict(provider='camera', resource='vision', service='vision-service',
                    configuration={'custom': 'preserved'}, capacity=3)
        old = {name: self.call('cap.offer', dict(args, contract=name))
               for name in ('one:1', 'two:1', 'three:1')}
        self.call('cap.offer', dict(args, contract='two:1'))
        other = self.call('cap.offer', dict(args, contract='one:1', resource='another'))
        withdraw = {k:args[k] for k in ('provider','resource','service')}
        withdraw['registrations'] = {c:old[c]['registration'] for c in ('one:1','two:1')}
        reply = self.call('cap.withdraw', withdraw, 'stop')
        self.assertEqual(reply, {'withdrawn':['one:1'], 'superseded':['two:1']})
        rows = {v['id']:v for v in self.call('cap.discover')['offers']}
        self.assertEqual(rows[old['one:1']['id']]['state'],'offline')
        self.assertEqual(rows[old['one:1']['id']]['configuration']['custom'], 'preserved')
        self.assertEqual(rows[old['one:1']['id']]['capacity'],3)
        self.assertTrue(all(rows[k]['state']=='ready' for k in (old['two:1']['id'], old['three:1']['id'],other['id'])))
        self.call('cap.offer',dict(args,contract='one:1'))
        self.assertEqual(self.call('cap.withdraw',withdraw,'stop'),reply)
        self.assertEqual(self.call('cap.withdraw',withdraw)['withdrawn'],[])
        self.assertTrue(all(v['state']=='ready' for v in self.call('cap.discover')['offers']))

    def test_legacy_rows_and_complete_provider_contract_resource_authorization(self):
        for name in ('one','two'):
            self.call('cap.define',{'name':name,'inputs':{},'outputs':{}})
        args=dict(provider='camera',resource='vision',service='vision-service')
        rows=[self.call('cap.offer',dict(args,contract=c)) for c in ('one:1','two:1')]
        db=self.a.stores['site']
        def make_legacy(tx):
            row=tx.need('offers',rows[0]['id']);row.pop('registration',None);tx.put('offers',row['id'],row)
        db.execute(make_legacy)
        withdrawal=dict(args,registrations={'one:1':None,'two:1':rows[1]['registration']})
        for actor, perms in [('camera',{'cap.offer','resource:vision','offer:one:1'}),
                             ('other',{'cap.offer','resource:vision','offer:one:1','offer:two:1'}),
                             ('camera',{'cap.offer','resource:other','offer:one:1','offer:two:1'})]:
            with self.assertRaises(Fault):
                db.execute(lambda tx:invoke(tx,Context(self.a,actor,'site',perms),'cap.withdraw',withdrawal))
        self.assertTrue(all(v['state']=='ready' for v in self.call('cap.discover')['offers']))
        result=db.execute(lambda tx:invoke(tx,Context(self.a,'camera','site',
            {'cap.offer','resource:vision','offer:one:1','offer:two:1'}),'cap.withdraw',withdrawal))
        self.assertEqual(result['withdrawn'],['one:1','two:1'])
