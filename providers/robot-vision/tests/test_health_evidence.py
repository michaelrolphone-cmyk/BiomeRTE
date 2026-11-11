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
                    def __init__(self,*args):pass
                    def query(self,*args):
                        queried.set()
                        raise adapter.ServiceError('service_unavailable','No completed health reply') from cause
                class Worker:
                    def __init__(self,*args):self.db=types.SimpleNamespace(close=lambda:None)
                    def once(self,pool):
                        queried.wait(.5);advertised.wait(.1)
                        closing.set();handlers[adapter.signal.SIGTERM](None,None)
                class Pool:
                    def __init__(self,*args):self.executor=types.SimpleNamespace(shutdown=lambda **kwargs:None)
                class Streams:
                    def __init__(self,*args,sequence_path=None):sequence_paths.append(sequence_path)
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
