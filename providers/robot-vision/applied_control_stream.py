"""Passive subscriber to the shared applied-control lane. Never sends control."""
import asyncio
import hashlib
import hmac
import json
import logging
from pathlib import Path
import sys
import threading
import time

class AppliedControlStream:
    def __init__(self, detector, config=None, on_control=None):
        self.detector=detector
        self.on_control=on_control
        self.config=dict(config or {})
        self.stop=threading.Event()
        self.thread=None

    def start(self):
        if self.config.get('enabled',True):
            self.thread=threading.Thread(target=self._run,daemon=True,name='motion-applied-control')
            self.thread.start()
        return self

    def _run(self):
        asyncio.run(self._observe())

    async def _observe(self):
        from aiohttp import ClientSession, ClientTimeout, WSMsgType
        sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
        from biomerte.stream_router import decode
        while not self.stop.is_set():
            try:
                config=json.loads((Path.home()/'.local/share/biomerte-controller/node.json').read_text())
                token=hmac.new(config['stream_router_secret'].encode(),b'rover-site',hashlib.sha256).hexdigest()
                async with ClientSession(headers={'Authorization':'Bearer '+token},
                        timeout=ClientTimeout(total=None,sock_connect=2)) as session:
                    async with session.ws_connect(self.config.get('url','http://127.0.0.1:8093/api/live/control'),
                            heartbeat=20,max_msg_size=4194304) as socket:
                        while not self.stop.is_set():
                            try:message=await socket.receive(timeout=1)
                            except asyncio.TimeoutError:continue
                            if message.type in (WSMsgType.TEXT,WSMsgType.BINARY):
                                value,_=decode(message.data,4194304)
                                issued=value.get('issued_utc_ms')
                                if type(issued) in (int,float):
                                    value['host_issue_age_s']=time.time()-issued/1000.
                                self._deliver(value)
                            elif message.type in (WSMsgType.CLOSED,WSMsgType.CLOSE,WSMsgType.ERROR):
                                break
            except Exception as error:
                logging.getLogger(__name__).debug('Applied control subscription: %s',type(error).__name__)
            finally:
                self._deliver({'type':'gap'})
            for _ in range(10):
                if self.stop.is_set():break
                await asyncio.sleep(.1)

    def _deliver(self,value):
        received=time.monotonic()
        self.detector.control(value,received)
        if self.on_control:self.on_control(value,received)

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=3)
