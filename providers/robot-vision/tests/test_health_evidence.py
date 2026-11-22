import pathlib,sys,tempfile,threading,types,unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
import biome_provider as adapter

class HealthEvidenceTests(unittest.TestCase):
    def test_input_loss_keeps_running_stream_provider_available(self):
        cfg={'provider':'provider','resource':'camera','slots':4}
        status={'availability':'unavailable','state':'running',
                'streaming':{'available':True,'lanes':['vision','map']}}
        self.assertEqual('ready',adapter.stream_offer(cfg,status)['state'])
        status['state']='stopping'
        self.assertEqual('offline',adapter.stream_offer(cfg,status)['state'])

    def test_unknown_health_does_not_withdraw_but_confirmed_absence_does(self):
        for cause,withdraws in [(TimeoutError(),False),(ConnectionRefusedError(),True),(FileNotFoundError(),True)]:
            with self.subTest(cause=type(cause).__name__):
                queried=threading.Event();advertised=threading.Event();closing=threading.Event()
                handlers={};offline=[];sequence_paths=[]
                class Remote:
                    def __init__(self,cfg):pass
                    def call(self,operation,args):
                        if operation=='cap.offer' and args['state']=='offline' and not closing.is_set():
                            offline.append(args['contract']);advertised.set()
                        return {}
                class Client:
                    def __init__(self,*args,**kwargs):pass
                    def query(self,*args):
                        queried.set()
                        raise adapter.ServiceError('service_unavailable','No completed health reply') from cause
                class Worker:
                    def __init__(self,*args,**kwargs):self.db=types.SimpleNamespace(close=lambda:None)
                    def recover_result_acknowledgements(self, **kwargs):pass
                    def once(self,pool,*,reconcile_receipts=True):
                        queried.wait(.5);advertised.wait(.1)
                        closing.set();handlers[adapter.signal.SIGTERM](None,None)
                class Pool:
                    def __init__(self,*args,**kwargs):self.executor=types.SimpleNamespace(shutdown=lambda **kwargs:None)
                class Streams:
                    def __init__(self,*args,sequence_path=None,rear_resource=None,host_io_config=None):sequence_paths.append(sequence_path)
                    def update(self,*args):pass
                    def close(self):pass
                with tempfile.TemporaryDirectory() as folder:
                    cfg={'state':folder,'repo':str(pathlib.Path(__file__).resolve().parents[3]),
                         'socket':'unused','provider':'provider','site':'site','slots':1,
                         'resource':'camera','python':sys.executable}
                    with patch.object(adapter,'Remote',Remote),patch.object(adapter,'VisionClient',Client),\
                         patch.object(adapter.signal,'signal',lambda kind,fn:handlers.__setitem__(kind,fn)),\
                         patch('biomerte.worker.Worker',Worker),patch('biomerte.worker.ActionPool',Pool),\
                         patch('leased_streams.VisionStreams',Streams):
                        adapter.serve(cfg)
                self.assertEqual([pathlib.Path(folder)/'provider/stream-sequences.sqlite'],sequence_paths)
                self.assertEqual(withdraws,bool(offline))

    def test_unrelated_definition_does_not_block_healthy_query_offers(self):
        cfg = {'provider':'provider', 'site':'site', 'resource':'camera', 'slots':1}
        wanted = {'robot.vision.metadata:1', 'robot.vision.status:1'}
        unrelated = next(k for k in sorted(adapter.CONTRACTS) if k not in wanted)
        ready = wanted | {unrelated}
        published = []
        class Remote:
            def call(self, name, args):
                if name == 'cap.define' and args['name'] == adapter.CONTRACTS[unrelated]['name']:
                    raise TimeoutError('unrelated definition')
                if name == 'cap.offer':
                    published.append((args['contract'], args['state']))
                return {}
        with tempfile.TemporaryDirectory() as temp:
            registry = pathlib.Path(temp) / 'contract-cache'
            known = set(ready)
            registered, advertised = set(), {}
            with patch('builtins.print'):
                retry = adapter.reconcile_query_offers(Remote(), cfg, ready, registered, known,
                                                       advertised, registry, threading.Event())
            self.assertTrue(retry)
            self.assertEqual(set(published), {(name,'ready') for name in wanted})
            self.assertEqual(registered, wanted)
            self.assertEqual(advertised, {k:'ready' for k in wanted})
            ready.remove('robot.vision.status:1')
            with patch('builtins.print'):
                adapter.reconcile_query_offers(Remote(), cfg, ready, registered, known,
                                               advertised, registry, threading.Event())
            self.assertEqual(published[-1], ('robot.vision.status:1','offline'))

    def test_real_advertisement_loop_restores_queries_when_relay_registry_is_unavailable(self):
        advertised = threading.Event()
        blocked_relay = threading.Event()
        handlers = {}
        target = 'robot.vision.metadata:1'
        class Remote:
            def __init__(self, cfg): pass
            def call(self, name, args):
                if name == 'cap.registrations':
                    error = RuntimeError('Unknown operation')
                    error.status = 404
                    raise error
                if name == 'cap.define' and args['name'] == 'relay.fixture':
                    blocked_relay.set()
                    raise TimeoutError('relay unavailable')
                if name == 'cap.offer' and args['contract'] == target and args['state'] == 'ready':
                    advertised.set()
                return {}
        class Client:
            def __init__(self, *args): pass
            def query(self, *args):
                return {'state':'running','capabilities':[target],
                        'streaming':{'available':True,'lanes':['vision','map'],'rear_enabled':True}}
        class Worker:
            def __init__(self,*args): self.db = types.SimpleNamespace(close=lambda:None)
            def recover_result_acknowledgements(self,**kwargs): pass
            def once(self,*args,**kwargs):
                blocked_relay.wait(2)
                handlers[adapter.signal.SIGTERM](None,None)
        class Streams:
            def __init__(self,*args,**kwargs): pass
            def update(self,*args): pass
            def close(self): pass
        with tempfile.TemporaryDirectory() as folder:
            root=pathlib.Path(folder)
            (root/'rover-relay-contracts.json').write_text('[{"name":"relay.fixture","version":1}]')
            cfg={'state':folder,'repo':str(pathlib.Path(__file__).resolve().parents[3]),
                 'socket':'unused','provider':'provider','site':'site','slots':1,
                 'resource':'camera','python':sys.executable}
            with patch.object(adapter,'HERE',root),patch.object(adapter,'Remote',Remote), \
                 patch.object(adapter,'VisionClient',Client), patch.object(adapter.signal,'signal',handlers.__setitem__), \
                 patch('biomerte.worker.Worker',Worker),patch('leased_streams.VisionStreams',Streams),patch('builtins.print'):
                adapter.serve(cfg)
        self.assertTrue(blocked_relay.is_set())
        self.assertTrue(advertised.is_set())
