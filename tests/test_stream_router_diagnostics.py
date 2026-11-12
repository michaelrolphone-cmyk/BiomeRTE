import json
import unittest
from biomerte.stream_router import Router, Session
from biomerte.store import Fault

class RouterDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_authority_read_failure_closes_session_and_retains_safe_reason(self):
        def unavailable(sid):
            raise Fault('private secret must not enter diagnostics',503)
        node='a'*64
        router=Router({node:'secret'},{node:unavailable})
        session=Session({'id':'b'*32,'authority':node,'attachment_credential':'sensitive'})
        router.sessions[session.grant['id']]=session
        await router.watch(session)
        self.assertTrue(session.closed)
        health=router.health()
        self.assertEqual(health['sessions'],0)
        self.assertEqual(health['recent_errors'][0]['status'],503)
        self.assertEqual(health['recent_errors'][0]['stage'],'authority_validation')
        self.assertNotIn('sensitive',json.dumps(health))
        self.assertNotIn('private secret',json.dumps(health))

    async def test_peer_inventory_distinguishes_server_socket_from_provider_presence(self):
        node='a'*64;router=Router({node:'secret'}, {})
        session=Session({'id':'b'*32,'authority':node})
        session.peers[('server','control','consumer-one')]=object()
        router.sessions[session.grant['id']]=session
        health=router.health()
        self.assertEqual(health['stream_peers'][node]['server'],{'control':1})
        self.assertEqual(health['stream_peers'][node]['provider'],{})

    async def test_wire_rejections_use_fixed_safe_reason_codes(self):
        router=Router({}, {})
        router.report('attachment',None,Fault('Expired or invalid driving intent',409))
        event=router.health()['recent_errors'][0]
        self.assertEqual(event['reason'],'invalid_intent_freshness')
