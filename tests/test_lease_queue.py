import os
import unittest
from biomerte.app import Application
from biomerte.store import Fault
from tests import test_runtime


class LeaseQueue(unittest.TestCase):
    setUp = test_runtime.Runtime.setUp
    call = test_runtime.Runtime.call

    def advertise(self, cap, state='ready', capacity=1):
        self.call('cap.define', {'name':cap, 'inputs':{}, 'outputs':{}})
        return self.call('cap.offer', {'provider':'device', 'contract':cap+':1', 'resource':'port',
                                       'capacity':capacity, 'state':state})

    def acquire(self, cap, task):
        return self.call('cap.acquire', {'task':task,'contract':cap+':1','reply_to':'in.'+task})

    def fence(self, lease):
        self.call('cap.fenced', {'provider':'device','generation':lease['generation'],'lease':lease['id']})

    def test_bounded_fifo_across_contracts_and_restart(self):
        self.advertise('first',capacity=2)
        self.advertise('second',capacity=2)
        a, b, c, d, e = [self.acquire(cap,t) for cap,t in [
            ('first','a'),('second','b'),('second','c'),('first','d'),('first','e')]]
        self.assertNotEqual(a['guard'],b['guard'])
        self.assertEqual([c['state'],d['state'],e['state']],['queued']*3)
        self.call('cap.release',{'lease':d['id']})
        self.a = Application(self.home)
        self.call('cap.release',{'lease':a['id']})
        granted=self.call('cap.lease',{'lease':c['id']})
        self.assertEqual(granted['state'],'fencing')
        self.assertGreater(granted['generation'],a['generation'])
        self.assertEqual(self.call('bus.inbox',{'address':'in.c'}),[])
        self.fence(granted)
        self.fence(granted)
        msgs=self.call('bus.inbox',{'address':'in.c'})
        self.assertEqual([m['body']['id'] for m in msgs],[c['id']])
        self.assertEqual(self.call('cap.lease',{'lease':e['id']})['state'],'queued')
        self.assertEqual(self.call('cap.lease',{'lease':d['id']})['state'],'released')
        self.assertEqual(self.acquire('second','c')['id'],granted['id'])
        with self.assertRaises(Fault):self.fence(a)

    def test_exclusive_unbounded_and_offline_wait(self):
        self.advertise('thing',state='offline',capacity=0)
        waiters=[self.acquire('thing',str(i)) for i in range(6)]
        self.assertTrue(all(l['state']=='queued' for l in waiters))
        self.advertise('thing',capacity=0)
        self.a.tick('site')
        leases=self.call('cap.leases',{})
        self.assertEqual(len(set(l['guard'] for l in leases)),6)
        self.assertTrue(all(l['state']=='fencing' for l in leases))
        self.advertise('thing',capacity=1)
        waiter=self.acquire('thing','last')
        for l in leases[:-1]:
            self.call('cap.release',{'lease':l['id']})
            self.assertEqual(self.call('cap.lease',{'lease':waiter['id']})['state'],'queued')
        self.call('cap.release',{'lease':leases[-1]['id']})
        self.assertEqual(self.call('cap.lease',{'lease':waiter['id']})['state'],'fencing')

    def test_signed_provider_ack_and_legacy_result_lookup(self):
        from biomerte.worker import Worker
        from tests.test_operations import LocalRemote
        self.advertise('pin')
        a=self.acquire('pin','job')
        peer=self.call('peer.trust',{'label':'mine','public_key':self.a.config['public_key'],
                                       'scopes':['cap.fenced']})
        self.call('cap.offer',{'provider':peer['id'],'contract':'pin:1','resource':'signed'})
        z=self.call('cap.acquire',{'task':'signed','contract':'pin:1','resource':'signed'})
        with self.assertRaises(Fault):
            self.call('cap.fenced',{'lease':z['id'],'provider':peer['id'],'generation':z['generation']})
        self.fence(a)
        import sys
        w=Worker(os.path.join(self.tmp.name,'fences.sqlite'),LocalRemote(self.a,'device'),
                 [{'contract':'pin:1','resource':'port','argv':[sys.executable,'-c','print("{}")']}])
        self.call('cap.invoke',{'operation':'unique','lease':a['id'],'inputs':{}})
        w.once()
        w.db.close()
        self.call('cap.release',{'lease':a['id']})
        self.assertEqual(self.call('cap.status',{'operation':'unique'})['state'],'succeeded')
