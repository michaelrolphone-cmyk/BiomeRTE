"""In-memory fanout of the existing processed frame; no per-frame RPC or artifact."""
import contextlib
import json
import queue
import struct
import threading


def packet(metadata, image=b''):
    raw=json.dumps(metadata,separators=(',',':'),allow_nan=False).encode()
    value=struct.pack('!I',len(raw))+raw+image
    return struct.pack('!I',len(value))+value


class LiveFrames:
    def __init__(self, maximum_subscribers=8):
        self.lock=threading.Lock()
        self.subscribers=set()
        self.maximum_subscribers=maximum_subscribers
        self.closed=threading.Event()

    @contextlib.contextmanager
    def subscribe(self):
        inbox=queue.Queue(1)
        with self.lock:
            if len(self.subscribers)>=self.maximum_subscribers:
                raise RuntimeError('live_subscriber_limit')
            self.subscribers.add(inbox)
        try:
            yield inbox
        finally:
            with self.lock:
                self.subscribers.discard(inbox)

    def publish(self, metadata, image):
        with self.lock:
            if not self.subscribers:return
        value=packet(metadata,image)
        with self.lock:
            for inbox in self.subscribers:
                if inbox.full():
                    inbox.get_nowait()
                inbox.put_nowait(value)

    def close(self):
        self.closed.set()
