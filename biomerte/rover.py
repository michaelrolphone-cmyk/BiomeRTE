"""Paired-server lease and stream ownership, independent of browser lifetimes."""
import asyncio
import contextlib
import json
import time
from .stream_router import decode


class Rover:
    def __init__(self, remote, resources, task='rover-server', require_provider_ready=False, clock=None):
        self.remote, self.resources, self.task = remote, resources, task
        self.clock = clock
        self.require_provider_ready = require_provider_ready
        self.descriptors = {}
        self.browsers = set()
        self.intent = asyncio.Queue(1)
        self.sequence = 0
        self.control_ready = False
        self.closed = False
        self.tasks = []
        self.map_head = None

    @property
    def control_ready(self):
        return self._control_ready and (self.clock is None or self.clock.ready)

    @control_ready.setter
    def control_ready(self, value):
        self._control_ready = value

    async def call(self, operation, **args):
        return await asyncio.to_thread(self.remote.call, operation, args)

    @contextlib.asynccontextmanager
    async def subscribe(self):
        # The HTTP adapter authenticates the browser before this call.
        queues = {lane: asyncio.Queue(1 if lane != 'map' else 8)
                  for lane in ('vision', 'map', 'control')}
        record = id(queues)
        member = (record, queues)
        self.browsers.add(record)
        if not hasattr(self, '_queues'):
            self._queues = {}
        self._queues[record] = queues
        if self.map_head is not None:
            queues['map'].put_nowait(json.dumps(dict(type='snapshot_required', **self.map_head)))
        try:
            yield queues
        finally:
            self.browsers.discard(record)
            self._queues.pop(record, None)

    def broadcast(self, lane, message):
        if lane in ('vision', 'map'):
            metadata, unused = decode(message, 4194304)
            revision = metadata.get('map_revision') if lane == 'vision' else metadata.get('revision')
            identity = metadata.get('map_id')
            if isinstance(identity, str) and type(revision) is int and revision >= 0:
                if (self.map_head is None or self.map_head['map_id'] != identity or
                        revision >= self.map_head['revision']):
                    self.map_head = {'map_id': identity, 'revision': revision}
        for queues in getattr(self, '_queues', {}).values():
            inbox = queues[lane]
            if inbox.full():
                if lane == 'map':
                    while not inbox.empty():inbox.get_nowait()
                    inbox.put_nowait(json.dumps(dict(type='snapshot_required', **(self.map_head or {}))))
                    continue
                inbox.get_nowait()
            inbox.put_nowait(message)

    def desired(self, mask, ttl_ms=250):
        if type(mask) is not int or not 0 <= mask <= 63 or type(ttl_ms) is not int or not 20 <= ttl_ms <= 1000:
            raise ValueError('Invalid desired output state')
        if not self.control_ready:
            raise RuntimeError('Control stream unavailable')
        self.sequence += 1
        now = self.clock.lower() if self.clock else time.time()
        value = dict(type='desired', sequence=self.sequence, outputs={'mask':mask},
                     issued_at=now, expires_at=now + ttl_ms/1000)
        if self.intent.full():self.intent.get_nowait()
        self.intent.put_nowait(value)
        return {'accepted_sequence':self.sequence,'received_or_applied':False}

    def stop(self):
        return self.desired(0)

    def discard_intent(self):
        while not self.intent.empty():self.intent.get_nowait()

    async def owner(self, contract, resource):
        # Only this server lifecycle acquires resources. Browser handlers never do.
        while not self.closed:
            lease = None
            descriptor = None
            lanes = []
            try:
                lease = await self.call('cap.acquire', contract=contract, resource=resource,
                                        task=self.task+':'+contract)
                while lease['state'] != 'bound':
                    if lease['state'] not in ('queued', 'fencing'):
                        raise ConnectionError('Lease unavailable')
                    await asyncio.sleep(.5)
                    lease = await self.call('cap.lease', lease=lease['id'])
                sessions = await self.call('stream.list', lease=lease['id'])
                for old in sessions:
                    await self.call('stream.close', session=old['id'])
                descriptor = await self.call('stream.open', lease=lease['id'])
                self.descriptors[contract] = descriptor
                lanes = [asyncio.create_task(self.maintain_lane(descriptor, lane))
                         for lane in descriptor['lanes']]
                await asyncio.gather(*lanes)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                for lane in lanes:
                    lane.cancel()
                await asyncio.gather(*lanes, return_exceptions=True)
                if self.descriptors.get(contract) is descriptor:
                    self.descriptors.pop(contract, None)
                    if descriptor is not None and 'map' in descriptor.get('lanes', []):
                        self.map_head = None
                        self.broadcast('map', json.dumps({'type':'snapshot_required'}))
                if descriptor is not None:
                    try:
                        await self.call('stream.close', session=descriptor['id'])
                    except Exception:
                        pass
                # Also release queued/fencing leases on shutdown or failed startup.
                # A retry must not queue behind capacity retained by this owner.
                if lease is not None:
                    try:
                        await self.call('cap.release', lease=lease['id'])
                    except Exception:
                        pass
            if not self.closed:
                await asyncio.sleep(.5)

    async def maintain_lane(self, descriptor, lane):
        # A transport outage is not a release of a persistent resource. Mint a
        # fresh attachment credential and reconnect this lane under its existing
        # epoch. Other lanes keep running. Only authoritative rejection returns
        # to the owner lifecycle to close the superseded session.
        while not self.closed:
            try:
                current = await self.call('stream.attach', session=descriptor['id'], role='server')
                await self.lane(current, lane)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                status = getattr(error, 'status', getattr(error, 'code', None))
                if status in (403, 404, 409, 410):
                    raise
                await asyncio.sleep(.5)

    async def lane(self, descriptor, lane):
        from websockets.asyncio.client import connect
        headers={'Authorization':'Stream '+descriptor['attachment_credential']}
        async with connect(descriptor['url']+'/'+lane,additional_headers=headers,max_queue=1,write_limit=65536) as socket:
            if lane=='control':
                self.discard_intent()
                self.control_ready=not self.require_provider_ready
                async def send():
                    while True:
                        intent=await self.intent.get()
                        now = self.clock.upper() if self.clock else time.time()
                        if not self.control_ready or intent['expires_at']<=now:continue
                        intent.update(authority=descriptor['authority'],generation=descriptor['generation'],epoch=descriptor['epoch'])
                        await asyncio.wait_for(socket.send(json.dumps(intent)),.1)
                task=asyncio.create_task(send())
            else:
                task=None
            async def receive():
                revision=None
                async for message in socket:
                    if lane=='control' and self.require_provider_ready:
                        metadata,_=decode(message,descriptor['limits']['max_message_bytes'])
                        if metadata.get('type')=='stream_status':
                            if self.require_provider_ready:
                                if any(metadata.get(k)!=descriptor[k] for k in ('authority','generation','epoch')):
                                    raise ValueError('Mismatched router peer status')
                                if any(type(metadata.get(k)) is not bool for k in ('provider_connected','server_connected')):
                                    raise ValueError('Invalid router peer status')
                                available=metadata.get('authority_available',True)
                                if type(available) is not bool:raise ValueError('Invalid authority availability')
                                ready=available and metadata['provider_connected'] and metadata['server_connected']
                                if not ready or not self.control_ready:
                                    self.discard_intent()
                                self.control_ready=ready
                            continue
                    if lane=='map':
                        metadata,_=decode(message,descriptor['limits']['max_message_bytes'])
                        if metadata.get('type')=='snapshot_required' or revision is not None and metadata.get('from_revision')!=revision:
                            self.broadcast('map',json.dumps({'type':'snapshot_required','map_id':metadata.get('map_id'),
                                                           'revision':metadata.get('revision')}))
                        revision=metadata.get('revision')
                    self.broadcast(lane,message)
            reader=asyncio.create_task(receive())
            tasks=[reader]+([task] if task else [])
            try:
                # Observe both directions: a dead sender cannot stay ready.
                done,pending=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
                for completed in done:completed.result()
            finally:
                if task:
                    self.control_ready=False
                    self.discard_intent()
                for pending in tasks:pending.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
        raise ConnectionError('Stream disconnected')

    async def start(self):
        self.tasks=[asyncio.create_task(self.owner(contract,resource)) for contract,resource in self.resources.items()]

    async def close(self):
        # Explicit server shutdown, never a browser disconnect.
        self.closed=True
        if self.control_ready:
            self.stop()
            await asyncio.sleep(.05)
        for task in self.tasks:task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        for descriptor in self.descriptors.values():
            try:await self.call('stream.close',session=descriptor['id'])
            except Exception:pass
            try:await self.call('cap.release',lease=descriptor['lease'])
            except Exception:pass


def web_app(rover, authenticate):
    """Mount on the paired server; authenticate uses the site's existing session."""
    from aiohttp import web
    async def allowed(request):
        if not await authenticate(request):
            raise web.HTTPUnauthorized()
    async def control(request):
        await allowed(request)
        body=await request.json()
        try:
            value=rover.stop() if body.get('stop') is True else rover.desired(body['mask'],body.get('fresh_ms',250))
            return web.json_response(value)
        except (ValueError,KeyError):
            raise web.HTTPBadRequest()
        except RuntimeError:
            raise web.HTTPServiceUnavailable()
    async def live(request):
        await allowed(request)
        lane=request.match_info['lane']
        if lane not in ('vision','map','control'):raise web.HTTPNotFound()
        socket=web.WebSocketResponse(heartbeat=20,max_msg_size=4096)
        await socket.prepare(request)
        async with rover.subscribe() as queues:
            async def send():
                while True:
                    message=await queues[lane].get()
                    if isinstance(message,bytes):await socket.send_bytes(message)
                    else:await socket.send_str(message if isinstance(message,str) else json.dumps(message))
            async def receive():
                async for ignored in socket:pass
            tasks=[asyncio.create_task(send()),asyncio.create_task(receive())]
            try:
                done,pending=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
                for task in done:task.result()
            finally:
                for task in tasks:task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
        return socket
    app=web.Application(client_max_size=4096)
    app.router.add_post('/api/controls',control)
    app.router.add_get('/api/live/{lane}',live)
    app.on_startup.append(lambda app:rover.start())
    app.on_cleanup.append(lambda app:rover.close())
    return app
