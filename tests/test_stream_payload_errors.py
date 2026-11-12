import asyncio
import base64
import hashlib
import hmac
import json
import time
import unittest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from biomerte.stream_router import Router, frame


class PayloadErrors(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.grant = dict(id='session', epoch='epoch', authority='controller', generation=4,
            contract='device.outputs.stream:1', resource='relay', lease='lease', provider='p',
            server='s', realm='site', site='site', state='open',
            limits=dict(max_message_bytes=4096, buffer_depth=8, messages_per_second=60),
            direction='bidirectional', delivery='ordered', lanes=['control'],
            expires_at=time.time()+60, credential_expires_at=time.time()+60)
        self.router = Router({'controller':'secret'}, {'controller':lambda _:self.grant})
        async def handler(socket):
            await self.router.attach(socket,socket.request.path,socket.request.headers['Authorization'])
        self.server=await serve(handler,'127.0.0.1',0)
        self.port=self.server.sockets[0].getsockname()[1]
        self.sockets=[]

    async def asyncTearDown(self):
        for socket in self.sockets: await socket.close()
        for task in self.router.tasks.values(): task.cancel()
        await asyncio.gather(*self.router.tasks.values(),return_exceptions=True)
        self.server.close();await self.server.wait_closed()

    async def attach(self,role):
        raw=json.dumps(dict(self.grant,role=role)).encode()
        token=base64.urlsafe_b64encode(raw).decode().rstrip('=')+'.'+hmac.new(b'secret',raw,hashlib.sha256).hexdigest()
        socket=await connect(f'ws://127.0.0.1:{self.port}/session/control',additional_headers={'Authorization':'Stream '+token})
        self.sockets.append(socket);return socket

    async def test_invalid_client_gets_reason_without_affecting_other_consumer(self):
        provider=await self.attach('provider');good=await self.attach('server');bad=await self.attach('server')
        await bad.send(json.dumps(dict(authority='controller',generation=4,epoch='epoch',sequence=1,type='pulse',direction='forward',duration_ms=400)))
        reply=json.loads(await asyncio.wait_for(bad.recv(),2))
        self.assertEqual('Expected desired output state',reply['reason'])
        self.assertFalse(reply['applied'])
        await bad.wait_closed();self.assertEqual(1008,bad.close_code)
        # Neutral desired output qualifies forwarding without actuating hardware.
        now=time.time()
        await good.send(json.dumps(dict(authority='controller',generation=4,epoch='epoch',sequence=1,type='desired',outputs={'mask':0},issued_at=now,expires_at=now+.4)))
        forwarded=json.loads(await asyncio.wait_for(provider.recv(),2))
        self.assertEqual({'mask':0},forwarded['outputs'])
        self.assertIsNone(good.close_code)

    async def test_invalid_binary_and_json_are_explained(self):
        for payload,code in [(b'bad',1008),('{',1007)]:
            socket=await self.attach('server');await socket.send(payload)
            reply=json.loads(await asyncio.wait_for(socket.recv(),2))
            self.assertEqual('STREAM_REJECTED',reply['code'])
            await socket.wait_closed();self.assertEqual(code,socket.close_code)
