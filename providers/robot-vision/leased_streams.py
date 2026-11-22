"""Attach the existing live processing feed to native leased WebSockets."""
import asyncio
import http.client
import json
import logging
import math
import socket
import struct
import threading
import time


class LocalConnection(http.client.HTTPConnection):
    def __init__(self,path):
        super().__init__('localhost',timeout=None)
        self.stream_socket=None
        self.path=path
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.sock.settimeout(None)
        self.stream_socket=self.sock
        self.sock.connect(self.path)

    def abort(self):
        # HTTP/1.0 getresponse() clears self.sock while its response body still
        # owns the descriptor. Retain that socket so lease loss can interrupt
        # an otherwise valid idle stream read.
        if self.stream_socket is not None:
            try:self.stream_socket.shutdown(socket.SHUT_RDWR)
            except OSError:pass
        self.close()


def exact(source,size):
    parts=[]
    while size:
        value=source.read(size)
        if not value:raise EOFError()
        parts.append(value);size-=len(value)
    return b''.join(parts)


def publication_grant(hint, descriptor):
    """Use the deadline from the current authorized attachment."""
    fields = ('id', 'authority', 'epoch', 'generation', 'contract', 'resource', 'provider')
    if any(descriptor.get(field) != hint.get(field) for field in fields):
        raise ValueError('Publisher attachment binding changed')
    deadline = descriptor.get('expires_at')
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or deadline <= time.time()):
        raise ValueError('Expired publisher attachment')
    return dict(hint, expires_at=deadline)


async def abort_on_wire_close(wire, local):
    await wire.wait_closed()
    local.abort()


class VisionStreams:
    def __init__(self,remote,socket_path,local_router_url=None,sequence_path=None,rear_resource=None):
        from biomerte.stream_inspection import loopback_url
        self.local_router_url=loopback_url(local_router_url)
        from stream_sequences import StreamSequences
        self.sequences=StreamSequences(sequence_path)
        self.rear_resource=rear_resource
        self.remote=remote
        self.socket_path=socket_path
        self.running={}
        self.reported={}
        self.connections={}
        self.lane_tasks={}

    def stop_lane(self, key, stop):
        stop.set()
        connection = self.connections.get(key)
        if connection:
            connection.abort()
        running = self.lane_tasks.get(key)
        if running:
            loop, task = running
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # The worker already closed its event loop.

    def update(self,grants):
        wanted={g['id']:g for g in grants if g['contract']=='robot.vision.stream:1'}
        wanted = {id+':'+lane:dict(grant, lane=lane) for id,grant in wanted.items() for lane in ('vision','map')}
        for id,(stop,thread) in list(self.running.items()):
            if id not in wanted:
                self.stop_lane(id, stop)
                if not thread.is_alive():self.running.pop(id,None)
        for id,grant in wanted.items():
            if id in self.running and self.running[id][1].is_alive():continue
            stop=threading.Event()
            thread=threading.Thread(target=lambda g=grant,s=stop:asyncio.run(self.run(g,s)),daemon=True)
            self.running[id]=(stop,thread)
            thread.start()

    async def run(self,grant,stop):
        from websockets.asyncio.client import connect
        from biomerte.stream_router import decode,frame
        key=grant['id']+':'+grant['lane']
        while not stop.is_set():
            local=None
            response=None
            active=None
            expiry=None
            disconnected=None
            stage='attachment'
            try:
                descriptor=await asyncio.to_thread(self.remote.call,'stream.attach',{'session':grant['id'],'role':'provider'})
                active=publication_grant(grant, descriptor)
                headers={'Authorization':'Stream '+descriptor['attachment_credential']}
                stage='public_socket'
                url=(self.local_router_url+'/'+grant['id'] if self.local_router_url else descriptor['url'])
                async with connect(url+'/'+grant['lane'],additional_headers=headers,max_size=None,max_queue=1,write_limit=65536) as wire:
                    self.lane_tasks[key] = (asyncio.get_running_loop(), asyncio.current_task())
                    stage='local_feed'
                    if stop.is_set():return
                    local=LocalConnection(self.socket_path)
                    self.connections[key]=local
                    if stop.is_set():return
                    deadline=active['expires_at']
                    expiry=asyncio.get_running_loop().call_later(max(0,deadline-time.time()),local.abort)
                    disconnected=asyncio.create_task(abort_on_wire_close(wire,local))
                    await asyncio.to_thread(local.request,'GET','/v1/live'+('/map' if grant['lane']=='map' else ''))
                    response=await asyncio.to_thread(local.getresponse)
                    if response.status!=200:raise RuntimeError('live_feed_unavailable')
                    while not stop.is_set():
                        stage='local_read'
                        header=await asyncio.to_thread(exact,response,4)
                        size=struct.unpack('!I',header)[0]
                        if not 4 <= size <= 16 * 1024 * 1024:raise ValueError('live_frame_size')
                        value=await asyncio.to_thread(exact,response,size)
                        metadata,image=decode(value,16 * 1024 * 1024,lane=grant["lane"])
                        # A slow transport resumes at fresh input rather than
                        # draining old images buffered before reconnection.
                        control = metadata.get('type') == 'sensor_manifest'
                        live = grant['lane'] == 'vision' and not control
                        if live and time.monotonic()-metadata['processed_monotonic']>1:continue
                        stage='sequence'
                        if time.time()>=deadline:break
                        sequence=self.sequences.next(active)
                        # Reserving a new range may wait for durable storage.
                        if live and time.monotonic()-metadata['processed_monotonic']>1:continue
                        metadata.update(authority=grant['authority'],generation=grant['generation'],
                                        epoch=grant['epoch'],sequence=sequence)
                        if live:
                            metadata['type']=('sensor_tick' if metadata.get('sensor_tick') is True
                                              and metadata.get('image_available') is False else 'frame')
                        from rover_wire import envelope
                        resource = grant['resource']
                        if self.rear_resource and resource == self.rear_resource and resource.endswith('.rear'):
                            resource = resource[:-5]
                        metadata = envelope(metadata, resource)
                        stage='publish'
                        await wire.send(frame(metadata,image))
            except asyncio.CancelledError:
                if stop.is_set():
                    return
                raise
            except Exception as error:
                if active is not None and time.time()>=active['expires_at']:
                    continue
                report_key=grant['lane']
                now=time.monotonic()
                if now-self.reported.get(report_key,-100)>30:
                    self.reported[report_key]=now
                    error_response=getattr(error,'response',None)
                    status=getattr(error,'status',getattr(error_response,'status_code',None))
                    logging.warning('Vision live lane %s stage=%s failed: %s status=%s',grant['lane'],stage,type(error).__name__,status)
                await asyncio.sleep(.5)
            finally:
                self.lane_tasks.pop(key, None)
                if expiry is not None:expiry.cancel()
                if disconnected is not None:
                    disconnected.cancel()
                    await asyncio.gather(disconnected,return_exceptions=True)
                if local:local.abort()
                if response is not None:response.close()
                if self.connections.get(key) is local:self.connections.pop(key,None)

    def close(self):
        for key, (stop, thread) in list(self.running.items()):self.stop_lane(key, stop)
        for stop,thread in self.running.values():thread.join()
        self.running.clear()
