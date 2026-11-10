"""Attach the existing live processing feed to native leased WebSockets."""
import asyncio
import http.client
import json
import logging
import socket
import struct
import threading
import time


class LocalConnection(http.client.HTTPConnection):
    def __init__(self,path):
        super().__init__('localhost',timeout=2)
        self.path=path
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.sock.settimeout(2)
        self.sock.connect(self.path)


def exact(source,size):
    parts=[]
    while size:
        value=source.read(size)
        if not value:raise EOFError()
        parts.append(value);size-=len(value)
    return b''.join(parts)


class VisionStreams:
    def __init__(self,remote,socket_path):
        self.remote=remote
        self.socket_path=socket_path
        self.running={}
        self.reported={}

    def update(self,grants):
        wanted={g['id']:g for g in grants if g['contract']=='robot.vision.stream:1'}
        wanted = {id+':'+lane:dict(grant, lane=lane) for id,grant in wanted.items() for lane in ('vision','map')}
        for id,(stop,thread) in list(self.running.items()):
            if id not in wanted:
                stop.set()
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
        sequence=0
        while not stop.is_set():
            local=None
            stage='attachment'
            try:
                descriptor=await asyncio.to_thread(self.remote.call,'stream.attach',{'session':grant['id'],'role':'provider'})
                headers={'Authorization':'Stream '+descriptor['attachment_credential']}
                stage='public_socket'
                async with connect(descriptor['url']+'/'+grant['lane'],additional_headers=headers,max_queue=1,write_limit=65536) as wire:
                    stage='local_feed'
                    local=LocalConnection(self.socket_path)
                    await asyncio.to_thread(local.request,'GET','/v1/live'+('/map' if grant['lane']=='map' else ''))
                    response=await asyncio.to_thread(local.getresponse)
                    if response.status!=200:raise RuntimeError('live_feed_unavailable')
                    while not stop.is_set():
                        stage='local_read'
                        header=await asyncio.to_thread(exact,response,4)
                        size=struct.unpack('!I',header)[0]
                        if not 4<=size<=4194304:raise ValueError('live_frame_size')
                        value=await asyncio.to_thread(exact,response,size)
                        metadata,image=decode(value,4194304)
                        # A slow transport resumes at fresh input rather than
                        # draining old images buffered before reconnection.
                        if grant['lane']=='vision' and time.monotonic()-metadata['processed_monotonic']>1:continue
                        sequence+=1
                        metadata.update(authority=grant['authority'],generation=grant['generation'],
                                        epoch=grant['epoch'],sequence=sequence)
                        if grant['lane']=='vision':metadata['type']='frame'
                        stage='publish'
                        await asyncio.wait_for(wire.send(frame(metadata,image)),1)
            except Exception as error:
                key=grant['lane']
                now=time.monotonic()
                if now-self.reported.get(key,-100)>30:
                    self.reported[key]=now
                    response=getattr(error,'response',None)
                    status=getattr(error,'status',getattr(response,'status_code',None))
                    logging.warning('Vision live lane %s stage=%s failed: %s status=%s',grant['lane'],stage,type(error).__name__,status)
                await asyncio.sleep(.5)
            finally:
                if local:local.close()

    def close(self):
        for stop,thread in self.running.values():stop.set()
        for stop,thread in self.running.values():thread.join()
        self.running.clear()
