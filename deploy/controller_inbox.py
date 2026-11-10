"""Nonblocking durable HTTP fallback with single-threaded capacity admission.
Python 3.4 compatible; job identities and completion fencing are unchanged.
"""
import concurrent.futures
import queue
import threading
import time


class ConcurrentInbox:
    def __init__(self, persistent, poll):
        self.persistent = persistent
        self.poll = poll
        self.pending = None

    def _poll(self, future):
        try:
            future.set_result(self.poll())
        except BaseException as error:
            future.set_exception(error)

    def next(self):
        if self.pending is not None and self.pending.done():
            completed = self.pending
            self.pending = None
            job = completed.result()
            if job.get('id'):
                return job
        job = self.persistent.next()
        if job is None:
            if self.pending is None:
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


def dispatch_loop(persistent, poll, process, capacity, ready, on_error, stopped=None):
    if type(capacity) is not int or capacity < 1:
        raise ValueError('Invalid controller capacity')
    inbox = ConcurrentInbox(persistent, poll)
    stopped = stopped if stopped is not None else threading.Event()
    retry_at = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=capacity) as pool:
        active = set()
        while not stopped.is_set():
            active = {future for future in active if not future.done()}
            if len(active) >= capacity or not ready():
                stopped.wait(.1)
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
                    active.add(pool.submit(process, job))
                else:
                    stopped.wait(.05 if persistent.socket is not None else .4)
            except Exception as error:
                on_error(error)
                retry_at = time.monotonic() + 2
