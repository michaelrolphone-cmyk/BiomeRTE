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
    def __init__(self, persistent, poll, wake=None):
        self.persistent = persistent
        self.poll = poll
        self.pending = None
        self.wake = wake or threading.Event()
        self.poll_after = 0

    def _poll(self, future):
        try:
            future.set_result(self.poll())
        except BaseException as error:
            future.set_exception(error)
        finally:
            self.wake.set()

    def next(self):
        if self.pending is not None and self.pending.done():
            completed = self.pending
            self.pending = None
            job = completed.result()
            if job.get('id'):
                return job
        if hasattr(self.persistent, 'jobs'):
            try:
                return self.persistent.jobs.get_nowait()
            except queue.Empty:
                pass
            job = None if time.monotonic() >= self.poll_after else {'idle': True}
        else:
            job = self.persistent.next()
        if job is None:
            if self.pending is None:
                interval = (getattr(self.persistent, 'http_poll_interval', 2.0)
                            if self.persistent.socket is not None else .4)
                self.poll_after = time.monotonic() + interval
                self.pending = concurrent.futures.Future()
                thread = threading.Thread(target=self._poll, args=(self.pending,))
                thread.daemon = True
                try:
                    thread.start()
                except BaseException:
                    self.pending = None
                    raise
            return {'idle': True}
        return job

    def wait_seconds(self):
        if self.pending is not None:
            return None
        return max(0, self.poll_after - time.monotonic())


def dispatch_loop(persistent, poll, process, capacity, ready, on_error, stopped=None):
    if type(capacity) is not int or capacity < 1:
        raise ValueError('Invalid controller capacity')
    wake = threading.Event()
    if hasattr(persistent, 'jobs'):
        persistent.jobs = WakeQueue(persistent.jobs, wake)
    inbox = ConcurrentInbox(persistent, poll, wake)
    stopped = stopped if stopped is not None else threading.Event()
    retry_at = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=capacity) as pool:
        active = set()
        while not stopped.is_set():
            # Clear before observation so publication cannot lose its wake.
            wake.clear()
            active = {future for future in active if not future.done()}
            if len(active) >= capacity or not ready():
                wake.wait(.1)
                continue
            try:
                if retry_at > time.monotonic():
                    try:
                        job = persistent.jobs.get(timeout=.1)
                    except queue.Empty:
                        job = {'idle': True}
                else:
                    job = inbox.next()
                if job.get('id'):
                    future = pool.submit(process, job)
                    active.add(future)
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
