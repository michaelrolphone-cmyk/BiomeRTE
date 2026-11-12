import pathlib,sys,tempfile,threading,types,unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
import biome_provider as adapter

class RegistrationIsolationTests(unittest.TestCase):
    def test_slow_registration_does_not_block_existing_stream_poll(self):
        active=threading.Event();release=threading.Event();handlers={};observed=[];sequence_paths=[];queries=[];feeds=[]
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
            def query(self,operation,inputs):
                queries.append(operation)
                if operation == 'robot.vision.status':
                    raise AssertionError('Readiness must not wait for SLAM diagnostics')
                return {'availability':'available','state':'running','capabilities':['robot.vision.status'],
                        'streaming':{'available':True,'lanes':['vision','map']}}
        class Connection:
            def __init__(self,*args):pass
            def request(self,method,path):feeds.append((method,path))
            def getresponse(self):return types.SimpleNamespace(status=200)
            def close(self):pass
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
                 patch.object(adapter,'UnixHTTPConnection',Connection),\
                 patch.object(adapter.signal,'signal',lambda kind,fn:handlers.__setitem__(kind,fn)),\
                 patch('biomerte.worker.Worker',Worker),patch('biomerte.worker.ActionPool',Pool),\
                 patch('leased_streams.VisionStreams',Streams):
                adapter.serve(cfg)
        self.assertEqual([pathlib.Path(folder)/'provider/stream-sequences.sqlite'],sequence_paths)
        self.assertEqual([True],observed,'Live polling waited behind unrelated registration')

        self.assertEqual(['robot.vision.metadata'],queries)
        self.assertEqual([('GET','/v1/live'),('GET','/v1/live/map')],feeds)
