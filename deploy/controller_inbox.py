"""Event-woken delivery with nonblocking durable HTTP recovery.
Python 3.4 compatible; job identities and completion fencing are unchanged.
"""
import concurrent.futures
import queue
import threading
import time


class WakeQueue:
    """Publish into the existing inbox, then wake its consumer."""
    def __init__(self, wrapped, wake):
        self.wrapped, self.wake = wrapped, wake

    def put(self, *args, **kwargs):
        result = self.wrapped.put(*args, **kwargs)
        self.wake.set()
        return result

    def put_nowait(self, value):
        return self.put(value, block=False)

    def __getattr__(self, name):
        return getattr(self.wrapped, name)


class ConcurrentInbox:
    """Concurrent recovery claims bounded by free execution slots."""
    def __init__(self, persistent, poll, wake=None, capacity=1):
        if type(capacity) is not int or capacity < 1:
            raise ValueError('Invalid HTTP claim capacity')
        self.persistent = persistent
        self.poll = poll
        self.capacity = capacity
        self._pending = []
        self.wake = wake or threading.Event()
        self.poll_after = 0

    @property
    def pending(self):
        # Preserve the single-slot adapter's inspection interface.
        return self._pending[0] if self._pending else None

    def _poll(self, future):
        try:
            future.set_result(self.poll())
        except BaseException as error:
            future.set_exception(error)
        finally:
            self.wake.set()

    def next(self, available=None, allow_poll=True):
        # Inspect every completed claim: a slow earlier request must not
        # hold a later response or a socket-delivered job behind it.
        for future in list(self._pending):
            if not future.done():
                continue
            self._pending.remove(future)
            try:
                job = future.result()
            except BaseException:
                self.poll_after = 0
                raise
            if job.get('id'):
                self.poll_after = 0
                return job
        if hasattr(self.persistent, 'jobs'):
            try:
                return self.persistent.jobs.get_nowait()
            except queue.Empty:
                pass
        else:
            job = self.persistent.next()
            if job is not None and job.get('id'):
                return job
        target = self.capacity if available is None else min(self.capacity, max(0, available))
        if allow_poll and time.monotonic() >= self.poll_after:
            interval = (getattr(self.persistent, 'http_poll_interval', 2.0)
                        if self.persistent.socket is not None else .4)
            self.poll_after = time.monotonic() + interval
            while len(self._pending) < target:
                future = concurrent.futures.Future()
                self._pending.append(future)
                thread = threading.Thread(target=self._poll, args=(future,))
                thread.daemon = True
                try:
                    thread.start()
                except BaseException:
                    self._pending.remove(future)
                    self.poll_after = 0
                    raise
        return {'idle': True}

    def wait_seconds(self):
        if self._pending:
            return None
        return max(0, self.poll_after - time.monotonic())


def dispatch_loop(persistent, poll, process, capacity, ready, on_error, stopped=None):
    if type(capacity) is not int or capacity < 1:
        raise ValueError('Invalid controller capacity')
    wake = threading.Event()
    if hasattr(persistent, 'jobs'):
        persistent.jobs = WakeQueue(persistent.jobs, wake)
    inbox = ConcurrentInbox(persistent, poll, wake, capacity)
    stopped = stopped if stopped is not None else threading.Event()
    retry_at = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=capacity) as pool:
        active = {}
        while not stopped.is_set():
            # Clear before observation so publication cannot lose its wake.
            wake.clear()
            active = {future: key for future, key in active.items() if not future.done()}
            if len(active) >= capacity or not ready():
                wake.wait(.1)
                continue
            try:
                job = inbox.next(capacity - len(active), time.monotonic() >= retry_at)
                if job.get('id'):
                    key = (job['id'], job.get('lease'))
                    if key in active.values():
                        continue
                    future = pool.submit(process, job)
                    active[future] = key
                    future.add_done_callback(lambda completed: wake.set())
                else:
                    delay = inbox.wait_seconds()
                    if retry_at > time.monotonic():
                        delay = retry_at - time.monotonic()
                    # Stop/readiness checks do not delay notified work.
                    wake.wait(.1 if delay is None else min(.1, delay))
            except Exception as error:
                on_error(error)
                retry_at = time.monotonic() + 2
