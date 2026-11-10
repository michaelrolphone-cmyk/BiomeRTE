"""Real peer loss gates control without releasing or replaying the lease."""
import asyncio
import base64
import hashlib
import hmac
import importlib.util
import json
import time
import unittest
from biomerte.rover import Rover
from biomerte.stream_router import Router
from biomerte.store import encode


@unittest.skipUnless(importlib.util.find_spec('websockets'), 'optional WebSocket transport unavailable')
class PeerReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_detach_gates_intent_and_reattach_never_replays(self):
        from websockets.asyncio.client import connect
        from websockets.asyncio.server import serve
        node='a'*64;secret='test-secret'*8
        grant=dict(id='b'*32,authority=node,generation=1,epoch='c'*32,
                   contract='device.outputs.stream:1',resource='relay',lease='lease',
                   provider='provider',server='server',realm='site',site='site',
                   state='open',role='server',lanes=['control'],direction='bidirectional',
                   delivery='supersedable',credential_expires_at=time.time()+60,
                   expires_at=time.time()+120,
                   limits=dict(max_message_bytes=2048,buffer_depth=1,messages_per_second=100))
        def token(role):
            raw=encode(dict(grant,role=role)).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip('=')+'.'+hmac.new(secret.encode(),raw,hashlib.sha256).hexdigest()
        router=Router({node:secret},{node:lambda sid:dict(grant)},peer_status=True)
        async def handle(ws):
            try:await router.attach(ws,ws.request.path,ws.request.headers.get('Authorization',''))
            except Exception:await ws.close(1008,'Rejected')
        async def until(predicate):
            for unused in range(200):
                if predicate():return
                await asyncio.sleep(.01)
            self.fail('Peer readiness transition did not arrive')
        async with serve(handle,'127.0.0.1',0) as server:
            base='ws://127.0.0.1:'+str(server.sockets[0].getsockname()[1])+'/'+grant['id']
            descriptor=dict(grant,url=base,attachment_credential=token('server'))
            rover=Rover(None,{},require_provider_ready=True)
            lane=asyncio.create_task(rover.lane(descriptor,'control'))
            try:
                await until(lambda:bool(router.sessions))
                self.assertFalse(rover.control_ready)
                with self.assertRaises(RuntimeError):rover.desired(0)
                async with connect(base+'/control',additional_headers={'Authorization':'Stream '+token('provider')}) as peer:
                    await until(lambda:rover.control_ready)
                    with self.assertRaises(asyncio.TimeoutError):await asyncio.wait_for(peer.recv(),.05)
                    rover.desired(0,500)
                    value=json.loads(await asyncio.wait_for(peer.recv(),1))
                    self.assertEqual(value['outputs'],{'mask':0})
                    first=value['sequence']
                await until(lambda:not rover.control_ready)
                self.assertFalse(lane.done())
                self.assertTrue(rover.intent.empty())
                with self.assertRaises(RuntimeError):rover.desired(0)
                async with connect(base+'/control',additional_headers={'Authorization':'Stream '+token('provider')}) as peer:
                    await until(lambda:rover.control_ready)
                    with self.assertRaises(asyncio.TimeoutError):await asyncio.wait_for(peer.recv(),.05)
                    rover.desired(0,500)
                    value=json.loads(await asyncio.wait_for(peer.recv(),1))
                    self.assertGreater(value['sequence'],first)
            finally:
                lane.cancel();await asyncio.gather(lane,return_exceptions=True)
                for task in list(router.tasks.values()):task.cancel()
                await asyncio.gather(*list(router.tasks.values()),return_exceptions=True)
