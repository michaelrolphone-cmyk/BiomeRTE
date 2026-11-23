"""Fair latest-only multiplexing for front/rear imagery and independent sensor ticks."""
import contextlib,queue,threading,time
from collections import OrderedDict
from leased_publish import LiveFrames,packet
import publication_trace

class LatestPerSource:
    def __init__(self):self.values=OrderedDict();self.condition=threading.Condition()
    def put(self,key,value):
        with self.condition:self.values[key]=value;self.condition.notify()
    def get(self,timeout=None):
        end=None if timeout is None else time.monotonic()+timeout
        with self.condition:
            while not self.values:
                remaining=None if end is None else end-time.monotonic()
                if remaining is not None and remaining<=0:raise queue.Empty
                self.condition.wait(remaining)
            return self.values.popitem(last=False)[1]
class DualCameraFrames(LiveFrames):
    def __init__(self, observer=None):
        super().__init__()
        self.observe = observer
        self.manifest = None

    def publish_manifest(self, manifest):
        with self.lock:
            changed = self.manifest is None or self.manifest[0] != manifest["revision"]
            value = packet(manifest)
            self.manifest = (manifest["revision"], value)
            if changed:
                for inbox in self.subscribers:
                    inbox.put("manifest", value)

    @contextlib.contextmanager
    def subscribe(self):
        inbox=LatestPerSource()
        with self.lock:
            if len(self.subscribers)>=self.maximum_subscribers:raise RuntimeError("live_subscriber_limit")
            self.subscribers.add(inbox)
            if self.manifest is not None:inbox.put("manifest",self.manifest[1])
        try:yield inbox
        finally:
            with self.lock:self.subscribers.discard(inbox)
    def publish(self,metadata,image):
        metadata=dict(metadata,camera_id=metadata.get("camera_id","front"))
        with publication_trace.span("publish", metadata):
            if self.observe is not None:
                with publication_trace.span("publish.observe"):
                    self.observe(metadata,image)
            key=(metadata["camera_id"],"image" if image else "sensors")
            with self.lock:
                if not self.subscribers:return
            with publication_trace.span("publish.serialize"):
                value=packet(metadata,image)
            with publication_trace.span("publish.fanout"):
                with self.lock:
                    for inbox in self.subscribers:inbox.put(key,value)
class RearCameraFrames(DualCameraFrames):
    def __init__(self,shared):super().__init__();self.shared=shared
    def publish(self,metadata,image):
        metadata=dict(metadata,camera_id="rear")
        super().publish(metadata,image);self.shared.publish(metadata,image)
