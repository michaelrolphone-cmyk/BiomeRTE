import unittest
from biomerte.app import Application
from biomerte.store import Fault, digest
from tests.test_runtime import Runtime

class CapabilityPlacement(unittest.TestCase):
    setUp = Runtime.setUp
    call = Runtime.call

    def offers(self):
        self.call('cap.define', dict(name='host.terminal.run', inputs={'argv':'list'}, outputs={'stdout':'str'}))
        for provider, resource, host in [('p1','terminal.one','one.local'),('p2','terminal.two','two.local')]:
            self.call('cap.offer', dict(provider=provider, resource=resource, contract='host.terminal.run:1',
                      configuration={'execution_host':host}, capacity=1))

    def acquire(self, task, **extra):
        return self.call('cap.acquire', dict(task=task, contract='host.terminal.run:1', **extra))

    def submit(self, operation, **extra):
        return self.call('cap.submit', dict(operation=operation, contract='host.terminal.run:1',
                         inputs={'argv':['hostname']}, **extra))

    def test_one_contract_many_implementations_and_unpinned_usage(self):
        self.offers()
        found=self.call('cap.discover', {'contract':'host.terminal.run:1'})
        contract,=found['contracts']
        self.assertEqual(len(contract['implementations']),2)
        self.assertEqual({x['host'] for x in contract['implementations']},{'one.local','two.local'})
        self.assertNotIn('resource',contract['usage']['calls'][0]['arguments'])
        self.assertNotIn('placement',contract['usage']['calls'][0]['arguments'])

    def test_automatic_selection_uses_another_provider_when_first_is_full(self):
        self.offers()
        a=self.acquire('a')
        b=self.acquire('b')
        self.assertNotEqual(a['provider'],b['provider'])
        self.assertEqual(a['id'],digest(['a','host.terminal.run:1','']))

    def test_constraints_are_conjunctive_and_never_fall_back(self):
        self.offers()
        a=self.acquire('a',placement={'host':'TWO.LOCAL.','provider':'p2'})
        self.assertEqual(a['resource'],'terminal.two')
        b=self.acquire('b',placement={'host':'two.local'})
        self.assertEqual(b['state'],'queued')
        with self.assertRaises(Fault):
            self.acquire('c',placement={'host':'one.local','provider':'p2'},enqueue=False)
        with self.assertRaises(Fault):
            self.acquire('d',resource='terminal.one',placement={'resource':'terminal.two'})

    def test_placement_does_not_change_inputs_or_contract(self):
        self.offers()
        self.submit('job',placement={'host':'two.local'})
        sub=self.call('cap.submission',{'operation':'job'})
        if 'provider' not in sub:
            lease=self.call('cap.lease',{'lease':sub['lease']})
            self.call('cap.fenced',dict(provider=lease['provider'],lease=lease['id'],generation=lease['generation']))
        action=self.call('cap.status',{'operation':'job'})
        self.assertEqual(action['provider'],'p2')
        self.assertEqual(action['contract'],'host.terminal.run:1')
        self.assertEqual(action['inputs'],{'argv':['hostname']})

    def test_submit_without_resource_and_retry_cannot_move_execution(self):
        self.offers()
        original=self.submit('job')
        self.assertEqual(self.submit('job')['lease'],original['lease'])
        with self.assertRaises(Fault):
            self.submit('job',placement={'provider':'p2'})
        with self.assertRaises(Fault):
            self.submit('job',resource='terminal.two')
        leases=self.call('cap.leases',{})
        self.assertEqual(len([x for x in leases if x['task']=='job']),1)

    def test_queued_constraint_survives_restart_and_new_matching_provider(self):
        self.offers()
        a=self.acquire('wait',placement={'host':'future.local'})
        self.assertEqual(a['state'],'queued')
        self.a=Application(self.home)
        self.call('cap.offer',dict(provider='p3',resource='terminal.three',contract='host.terminal.run:1',
                  configuration={'execution_host':'future.local'}))
        self.a.tick('site')
        bound=self.call('cap.lease',{'lease':a['id']})
        self.assertEqual(bound['provider'],'p3')
        self.assertEqual(bound['placement'],{'host':'future.local'})

    def test_resource_alias_keeps_identity_and_release_cancels_waiter(self):
        self.offers()
        a=self.acquire('a',resource='terminal.two')
        b=self.acquire('a',placement={'resource':'terminal.two'})
        self.assertEqual(a['id'],b['id'])
        q=self.acquire('q',placement={'host':'two.local'})
        self.call('cap.release',{'lease':q['id']})
        self.call('cap.release',{'lease':a['id']})
        self.assertEqual(self.call('cap.lease',{'lease':q['id']})['state'],'released')

    def test_unknown_host_and_invalid_fields_are_not_guessed(self):
        self.offers()
        self.call('cap.offer',dict(provider='p3',resource='terminal.pi',contract='host.terminal.run:1'))
        with self.assertRaises(Fault):
            self.acquire('unknown',placement={'host':'pi'},enqueue=False)
        for placement in ({'host':''},{'host':3},{'system':'pi'},{'provider':''},{'host':'bad host'}):
            with self.assertRaises(Fault):
                self.acquire('bad',placement=placement)

    def test_prepare_filters_activation_to_requested_host(self):
        self.offers()
        self.assertEqual(self.call('cap.prepare',dict(contract='host.terminal.run:1',
                         placement={'host':'missing.local'}))['state'],'missing')
        ready=self.call('cap.prepare',dict(contract='host.terminal.run:1',placement={'provider':'p2'}))
        offers=self.call('cap.discover',{'contract':'host.terminal.run:1'})['offers']
        self.assertEqual(next(x for x in offers if x['id']==ready['offer'])['provider'],'p2')
