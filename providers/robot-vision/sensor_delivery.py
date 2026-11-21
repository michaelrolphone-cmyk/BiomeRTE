"""Time-windowed handoff for live sensor consumers."""
import collections
import threading
import time


def freshness_window(service):
    from vision_config import DEFAULT_CONFIG
    return float(getattr(service, "config", {}).get(
        "freshness_threshold_s", DEFAULT_CONFIG["freshness_threshold_s"]))


class RecentReadings:
    """The publisher only appends. Each consumer discards its expired readings."""
    def __init__(self, window, clock=time.monotonic):
        self.window = window
        self.clock = clock
        self.ready = threading.Condition()
        self.pending = collections.deque()
        self.closed = False
        self.expired = 0

    def _expire(self):
        cutoff = self.clock() - self.window
        while self.pending and self.pending[0][0] < cutoff:
            self.pending.popleft()
            self.expired += 1

    def put(self, captured, item):
        with self.ready:
            if self.closed:
                return
            self._expire()
            if captured < self.clock() - self.window:
                self.expired += 1
                return
            self.pending.append((captured, item))
            self.ready.notify()

    def get(self):
        with self.ready:
            while not self.closed:
                self._expire()
                if self.pending:
                    return self.pending.popleft()
                self.ready.wait()
            return None

    def close(self):
        with self.ready:
            self.closed = True
            self.pending.clear()
            self.ready.notify_all()
