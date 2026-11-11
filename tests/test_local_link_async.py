import importlib.util
import json
import pathlib
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

class LocalAsyncAdapterTests(unittest.TestCase):
    def test_local_link_uses_shared_nonblocking_dispatcher(self):
        root=pathlib.Path(__file__).resolve().parents[1]
        spec=importlib.util.spec_from_file_location('qualified_local_link',str(root/'deploy/local_link.py'))
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        calls=[];requests=[];transport=object()
        def dispatcher(persistent,poll,process,capacity,ready,on_error):
            self.assertIs(persistent,transport);self.assertEqual(capacity,3)
            self.assertFalse(ready());self.assertTrue(callable(process));self.assertTrue(callable(on_error))
            self.assertEqual(poll(),{'idle':True});calls.append('dispatched')
        class Thread:
            def __init__(self,**kwargs):pass
            def start(self):pass
        with tempfile.TemporaryDirectory() as home:
            pathlib.Path(home,'node.json').write_text(json.dumps({'node':'app','gateway_link':{'controller_id':'mac','capacity':3}}))
            args=types.SimpleNamespace(home=home,port=8890,key=home+'/bridge.key')
            def fetch(url,payload=None,token=None,**kwargs):
                requests.append((url,payload,token,kwargs));return {'idle':True}
            mocks={'persistent_link':types.SimpleNamespace(ControllerLink=lambda *a:transport),
                   'result_delivery':types.SimpleNamespace(complete=lambda *a:None),
                   'controller_inbox':types.SimpleNamespace(dispatch_loop=dispatcher)}
            with patch.dict(sys.modules,mocks),patch.object(module.threading,'Thread',Thread):
                module.run(args,'https://example.invalid','existing-token','existing-access',fetch,None,None,None)
        self.assertEqual(calls,['dispatched'])
        self.assertEqual(requests,[('https://example.invalid/bridge/next',{},'existing-token',{'site_access':'existing-access'})])

if __name__=='__main__':unittest.main()
