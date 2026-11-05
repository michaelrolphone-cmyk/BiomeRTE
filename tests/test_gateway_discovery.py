import importlib.util, io, json, pathlib, sys, unittest
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'deploy'))
spec=importlib.util.spec_from_file_location('gateway_discovery_test',str(ROOT/'deploy/gateway-agent.py'))
gateway=importlib.util.module_from_spec(spec);spec.loader.exec_module(gateway)

class GatewayDiscovery(unittest.TestCase):
    def test_discovery_uses_paged_operation(self):
        answer={'result':{'content':[{'type':'text','text':'{}'}]}}
        with patch.object(gateway,'fetch',return_value=answer) as fetch:
            gateway.dispatch({'id':'j','request':{'name':'biome_discover','arguments':{'cursor':'next'}}},'http://localhost',{'admin_token':'t'})
        self.assertEqual(fetch.call_args.args[1]['params']['arguments'],{'operation':'discovery.page','arguments':{'cursor':'next'}})
    def test_fetch_does_not_reject_large_valid_response(self):
        body=json.dumps({'value':'x'*500000}).encode()
        with patch.object(gateway.opener,'open',return_value=io.BytesIO(body)):
            self.assertEqual(len(gateway.fetch('http://localhost')['value']),500000)
    def test_status_does_not_conflate_readiness_and_business_success(self):
        values=[{'runtime':'up','realms':{'site':'ready'}},{'result':{'content':[{'type':'text','text':'{"state":"degraded"}'}]}}]
        with patch.object(gateway,'fetch',side_effect=values):
            r=gateway.dispatch({'id':'j','request':{'name':'biome_status'}},'http://localhost',{'node':'n','admin_token':'t'})
        health=json.loads(r['content'][0]['text'])['health']
        self.assertEqual(health['realms']['site'],'ready')
        self.assertEqual(health['consumer_health']['state'],'degraded')
