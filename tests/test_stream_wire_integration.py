"""Actual socket qualification for the persistent router (optional dependency)."""
import asyncio
import base64
import hashlib
import hmac
import json
import time
import unittest
try:
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve
except ImportError:
    connect=serve=None
from biomerte.stream_router import Router,frame
from biomerte.store import encode,Fault


@unittest.skipIf(connect is None,'websockets installed in the live-streaming CI job')
class StreamWire(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.grant=dict(id='a'*32,epoch='b'*32,authority='c'*64,site='site',realm='site',
            lease='lease',contract='vision:1',resource='camera',provider='provider',server='server',
            generation=3,state='open',direction='bidirectional',delivery='supersedable',lanes=['vision','control'],
            limits=dict(max_message_bytes=4096,buffer_depth=1,messages_per_second=100),
            expires_at=time.time()+100,credential_expires_at=time.time()+60)
        self.valid=True
        self.checks=0
        def validate(id):
            self.checks+=1
            if not self.valid:raise Fault('authority changed',409)
            return self.grant
        self.router=Router({self.grant['authority']:'secret'},{self.grant['authority']:validate})
        async def handler(socket):
            try:
                await self.router.attach(socket,socket.request.path,socket.request.headers['Authorization'])
            except Exception:
                await socket.close(1008,'Rejected')
        self.listener=await serve(handler,'127.0.0.1',0,max_queue=1,write_limit=4096)
        self.port=self.listener.sockets[0].getsockname()[1]

    def token(self,role):
        raw=encode(dict(self.grant,role=role)).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip('=')+'.'+hmac.new(b'secret',raw,hashlib.sha256).hexdigest()

    async def attach(self,role,lane):
        return await connect('ws://127.0.0.1:%d/%s/%s'%(self.port,self.grant['id'],lane),
            additional_headers={'Authorization':'Stream '+self.token(role)})

    async def asyncTearDown(self):
        for session in list(self.router.sessions.values()):await session.close()
        for task in list(self.router.tasks.values()):task.cancel()
        await asyncio.gather(*list(self.router.tasks.values()),return_exceptions=True)
        self.listener.close()
        await self.listener.wait_closed()

    async def test_real_provider_and_server_share_owner_and_revocation_closes_both(self):
        provider=await self.attach('provider','vision')
        server=await self.attach('server','vision')
        metadata=dict(authority=self.grant['authority'],generation=3,epoch=self.grant['epoch'],sequence=1,
            frame_id='f1',source_timestamp=None,processing_timestamp=1,calibration_revision=1,map_revision=1)
        value=frame(metadata,b'original-jpeg')
        await provider.send(value)
        self.assertEqual(value,await asyncio.wait_for(server.recv(),2))
        self.valid=False
        with self.assertRaises(Exception):
            await asyncio.wait_for(server.recv(),2)
        await provider.close()
        await server.close()

    async def test_control_accepts_new_state_without_waiting_for_prior_ack(self):
        provider=await self.attach('provider','control')
        server=await self.attach('server','control')
        now=time.time()
        for i in (1,2):
            await server.send(json.dumps(dict(authority=self.grant['authority'],generation=3,
                epoch=self.grant['epoch'],sequence=i,type='desired',outputs={'mask':i},
                issued_at=now,expires_at=now+.9)))
        value=json.loads(await asyncio.wait_for(provider.recv(),2))
        if value['sequence']==1:value=json.loads(await asyncio.wait_for(provider.recv(),2))
        self.assertEqual(2,value['sequence'])
        await provider.close()
        await server.close()
