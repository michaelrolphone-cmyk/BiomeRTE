"""Unreachable authority pauses control; confirmed revocation still closes it."""
import asyncio,base64,hashlib,hmac,importlib.util,json,socket,time,unittest,urllib.request
from types import SimpleNamespace
from unittest.mock import patch
from biomerte.rover import Rover
from biomerte.stream_router import Router,serve
from biomerte.store import encode


@unittest.skipUnless(importlib.util.find_spec('websockets'), 'optional WebSocket transport unavailable')
class AuthorityAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_quarantines_without_replay_and_release_revokes(self):
        from websockets.asyncio.server import serve as listen
        from websockets.asyncio.client import connect
        node='a'*64;secret='test-secret'*8
        grant=dict(id='b'*32,authority=node,generation=1,epoch='c'*32,
                   contract='device.outputs.stream:1',resource='relay',lease='lease',
                   provider='provider',server='server',realm='site',site='site',
                   state='open',role='server',lanes=['control'],direction='bidirectional',
                   delivery='supersedable',credential_expires_at=time.time()+60,
                   expires_at=time.time()+120,
                   limits=dict(max_message_bytes=2048,buffer_depth=1,messages_per_second=100))
        mode={'value':'open'}
        def validate(sid):
            if mode['value']=='unavailable':raise TimeoutError('untrusted diagnostic detail')
            return dict(grant,state='released' if mode['value']=='released' else 'open')
        def token(role):
            raw=encode(dict(grant,role=role)).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip('=')+'.'+hmac.new(secret.encode(),raw,hashlib.sha256).hexdigest()
        router=Router({node:secret},{node:validate},peer_status=True)
        async def handle(ws):
            try:await router.attach(ws,ws.request.path,ws.request.headers.get('Authorization',''))
            except Exception:await ws.close(1008,'Rejected')
        async def until(predicate):
            for unused in range(300):
                if predicate():return
                await asyncio.sleep(.01)
            self.fail('Expected control availability transition')
        async with listen(handle,'127.0.0.1',0) as server:
            base='ws://127.0.0.1:'+str(server.sockets[0].getsockname()[1])+'/'+grant['id']
            rover=Rover(None,{},require_provider_ready=True)
            task=asyncio.create_task(rover.lane(dict(grant,url=base,attachment_credential=token('server')),'control'))
            try:
                async with connect(base+'/control',additional_headers={'Authorization':'Stream '+token('provider')}) as peer:
                    await until(lambda:rover.control_ready)
                    mode['value']='unavailable'
                    await until(lambda:not rover.control_ready)
                    self.assertFalse(task.done())
                    self.assertIsNone(peer.close_code)
                    self.assertEqual(len(router.health()['quarantined_sessions']),1)
                    with self.assertRaises(RuntimeError):rover.desired(0)
                    mode['value']='open'
                    await until(lambda:rover.control_ready)
                    self.assertEqual(router.health()['quarantined_sessions'],[])
                    with self.assertRaises(asyncio.TimeoutError):await asyncio.wait_for(peer.recv(),.05)
                    rover.desired(0,500)
                    value=json.loads(await asyncio.wait_for(peer.recv(),1))
                    self.assertEqual(value['outputs'],{'mask':0})
                    mode['value']='released'
                    await asyncio.wait_for(peer.wait_closed(),3)
                    await until(task.done)
                    self.assertFalse(rover.control_ready)
            finally:
                task.cancel();await asyncio.gather(task,return_exceptions=True)
                for watcher in list(router.tasks.values()):watcher.cancel()
                await asyncio.gather(*list(router.tasks.values()),return_exceptions=True)

    async def test_private_health_declares_one_json_content_type(self):
        reserve=socket.socket();reserve.bind(('127.0.0.1',0))
        port=reserve.getsockname()[1];reserve.close()
        app=SimpleNamespace(node='a'*64,config={'stream_router_secret':'s'*40})
        with patch('biomerte.app.Application',return_value=app):
            task=asyncio.create_task(serve({'controllers':['unused'],'listen':'127.0.0.1',
                'port':port,'transport':'private_http','peer_status':True}))
            try:
                def read():
                    with urllib.request.urlopen('http://127.0.0.1:'+str(port)+'/health',timeout=1) as response:
                        return response.headers.get_all('Content-Type'),json.load(response)
                for unused in range(100):
                    try:headers,value=await asyncio.to_thread(read);break
                    except OSError:await asyncio.sleep(.01)
                else:self.fail('Health listener did not start')
                self.assertEqual(headers,['application/json'])
                self.assertEqual(value['state'],'ready')
            finally:
                task.cancel();await asyncio.gather(task,return_exceptions=True)
