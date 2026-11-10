import copy
import importlib.util
import os
import sys
import unittest
from unittest.mock import patch
from tests import test_runtime as runtime
from biomerte.identity import sign, verify
from biomerte.store import Fault
from biomerte.worker import Remote


class ControllerDestinationTests(unittest.TestCase):
    setUp=runtime.Runtime.setUp
    peer=runtime.Runtime.peer
    trust=runtime.Runtime.trust
    call=runtime.Runtime.call

    def request(self, peer, controller):
        payload={'site':self.a.site,'realm':'site','operation':'cap.discover','arguments':{},'id':'signed-destination'}
        if controller is not None:payload['controller']=controller
        return {'actor':peer.node,'signature':sign(peer.key,payload)}

    def test_explicit_destination_is_signed_and_verified(self):
        peer=self.peer();self.trust(self.a,peer,['cap.discover'])
        auth=self.request(peer,self.a.node)
        value=self.a.call('cap.discover',{},'signed-destination',auth=auth,controller=self.a.node)
        self.assertIn('offers',value)
        with self.assertRaises(Fault):self.a.call('cap.discover',{},'signed-destination',auth=auth,controller=peer.node)
        with self.assertRaises(Fault):self.a.call('cap.discover',{},'signed-destination',auth=auth)

    def test_legacy_signed_local_request_stays_supported(self):
        peer=self.peer();self.trust(self.a,peer,['cap.discover'])
        self.assertIn('offers',self.a.call('cap.discover',{},'signed-destination',auth=self.request(peer,None)))

    def test_worker_remote_signs_destination_without_rewriting_operation(self):
        peer=self.peer();captured=[]
        remote=Remote('http://127.0.0.1:9999',self.a.site,peer.node,peer.key,controller=self.a.node)
        with patch('biomerte.worker.post',side_effect=lambda *args:captured.append(args[2]) or {}):
            remote.call('cap.discover',{},'exact-id')
        wire=captured[0];auth=wire.pop('auth')
        self.assertEqual(wire['controller'],self.a.node)
        self.assertEqual(wire['id'],'exact-id')
        verify(peer.config['public_key'],auth['signature'],wire)
        wire['controller']=peer.node
        with self.assertRaises(Fault):verify(peer.config['public_key'],auth['signature'],wire)


class DispatchRoutingTests(unittest.TestCase):
    def test_internal_routing_fields_are_not_forwarded_to_mcp(self):
        path=os.path.join(os.path.dirname(__file__),'..','deploy','gateway-agent.py')
        spec=importlib.util.spec_from_file_location('destination_gateway_agent',path)
        module=importlib.util.module_from_spec(spec)
        with patch.object(sys,'path',[os.path.dirname(path)]+sys.path):
            spec.loader.exec_module(module)
        args={'controller':'node-a','controller_owner':'transport-a','operation':'cap.discover','arguments':{},'request_id':'r'}
        job={'id':'job','request':{'name':'biome_invoke','arguments':args}}
        with patch.object(module,'fetch',return_value={'result':{'ok':True}}) as fetch:
            self.assertEqual(module.dispatch(job,'http://127.0.0.1:1234',{'node':'node-a','admin_token':'test'}),{'ok':True})
            forwarded=fetch.call_args[0][1]['params']['arguments']
            self.assertNotIn('controller',forwarded);self.assertNotIn('controller_owner',forwarded)
        self.assertIn('controller',args)
        with self.assertRaises(ValueError):module.dispatch(job,'http://127.0.0.1:1234',{'node':'other','admin_token':'test'})
