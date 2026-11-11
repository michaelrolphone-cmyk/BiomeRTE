import pathlib,sys,tempfile,threading,types,unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
import biome_provider as adapter

class RegistrationIsolationTests(unittest.TestCase):
    def test_slow_registration_does_not_block_existing_stream_poll(self):
        active=threading.Event();release=threading.Event();handlers={};observed=[];sequence_paths=[]
        class Remote:
            def __init__(self,cfg):pass
            def call(self,operation,args):
                if operation=='cap.define':
                    active.set()
                    release.wait(.3)
                    active.clear()
                return {}
        class Client:
            def __init__(self,*args):pass
            def query(self,*args):
                return {'availability':'available','state':'running','capabilities':['robot.vision.status'],
                        'streaming':{'available':True,'lanes':['vision','map']}}
        class Worker:
            def __init__(self,*args):self.db=types.SimpleNamespace(close=lambda:None)
            def once(self,pool):
                active.wait(.5)
                observed.append(active.is_set())
                release.set()
                handlers[adapter.signal.SIGTERM](None,None)
        class Pool:
            def __init__(self,*args):self.executor=types.SimpleNamespace(shutdown=lambda **kwargs:None)
        class Streams:
            def __init__(self,*args,sequence_path=None):sequence_paths.append(sequence_path)
            def update(self,*args):pass
            def close(self):pass
        with tempfile.TemporaryDirectory() as folder:
            cfg={'state':folder,'repo':str(pathlib.Path(__file__).resolve().parents[3]),'socket':'unused',
                 'provider':'provider','site':'site','slots':1,'resource':'camera','python':sys.executable}
            with patch.object(adapter,'Remote',Remote),patch.object(adapter,'VisionClient',Client),\
                 patch.object(adapter.signal,'signal',lambda kind,fn:handlers.__setitem__(kind,fn)),\
                 patch('biomerte.worker.Worker',Worker),patch('biomerte.worker.ActionPool',Pool),\
                 patch('leased_streams.VisionStreams',Streams):
                adapter.serve(cfg)
        self.assertEqual([pathlib.Path(folder)/'provider/stream-sequences.sqlite'],sequence_paths)
        self.assertEqual([True],observed,'Live polling waited behind unrelated registration')
