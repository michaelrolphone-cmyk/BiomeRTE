"""Bounded revision-aware map delta feed."""
import contextlib
import queue
import threading
from leased_publish import packet


class LiveMap:
    def __init__(self):
        self.lock=threading.Lock()
        self.subscribers=set()
        self.current={"type":"snapshot_required","map_id":None,"revision":None}
        self.closed=threading.Event()

    @contextlib.contextmanager
    def subscribe(self):
        inbox=queue.Queue(8)
        with self.lock:
            if len(self.subscribers)>=8:raise RuntimeError("live_subscriber_limit")
            self.subscribers.add(inbox)
            inbox.put_nowait(packet(self.current))
        try:yield inbox
        finally:
            with self.lock:self.subscribers.discard(inbox)

    def publish(self,map_id,previous,revision,changes):
        with self.lock:
            self.current={"type":"snapshot_required","map_id":map_id,"revision":revision}
            value=packet({"type":"map_delta","map_id":map_id,"from_revision":previous,
                          "revision":revision,"changes":changes})
            if len(value)>1048576:value=packet(self.current)
            for inbox in self.subscribers:
                if inbox.full():
                    while not inbox.empty():inbox.get_nowait()
                    inbox.put_nowait(packet(self.current))
                else:inbox.put_nowait(value)

    def close(self):
        self.closed.set()
