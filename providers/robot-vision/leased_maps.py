"""Bounded revision-aware map delta feed."""
import contextlib
import queue
import threading
from leased_publish import packet


class LiveMap:
    def __init__(self, *, world=False):
        self.lock=threading.Lock()
        self.subscribers=set()
        self.current={"type":"snapshot_required","map_id":None,"revision":None}
        self.world_enabled = world
        self.world_snapshot = None
        self.closed=threading.Event()

    @contextlib.contextmanager
    def subscribe(self):
        inbox=queue.Queue(8)
        with self.lock:
            if len(self.subscribers)>=8:raise RuntimeError("live_subscriber_limit")
            self.subscribers.add(inbox)
            if self.world_snapshot is not None:
                inbox.put_nowait(self.world_snapshot)
            if not self.world_enabled:
                inbox.put_nowait(packet(self.current))
        try:yield inbox
        finally:
            with self.lock:self.subscribers.discard(inbox)

    def publish_world(self, value):
        with self.lock:
            self.world_snapshot = value
            for inbox in self.subscribers:
                if inbox.full():
                    while not inbox.empty():
                        try:
                            inbox.get_nowait()
                        except queue.Empty:
                            break
                    inbox.put_nowait(value)
                    inbox.put_nowait(packet(self.current))
                else:
                    inbox.put_nowait(value)

    def publish(self,map_id,previous,revision,changes):
        with self.lock:
            self.current={"type":"snapshot_required","map_id":map_id,"revision":revision}
            value=packet({"type":"map_delta","map_id":map_id,"from_revision":previous,
                          "revision":revision,"changes":changes})
            if len(value)>16392:value=packet(self.current)
            if self.world_enabled and self.world_snapshot is None:
                return
            for inbox in self.subscribers:
                if inbox.full():
                    bootstrap_pending = False
                    while not inbox.empty():
                        try:
                            bootstrap_pending |= inbox.get_nowait() is self.world_snapshot
                        except queue.Empty:
                            break
                    if bootstrap_pending:
                        inbox.put_nowait(self.world_snapshot)
                    inbox.put_nowait(packet(self.current))
                else:inbox.put_nowait(value)

    def close(self):
        self.closed.set()
