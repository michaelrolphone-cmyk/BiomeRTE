import os
import queue
import sys
import threading
import time
import unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'deploy'))
from controller_inbox import ConcurrentInbox, dispatch_loop

class Persistent:
    def __init__(self):
        self.jobs = queue.Queue()
        self.socket = None

class PooledClaimTests(unittest.TestCase):
    def test_all_slots_collect_concurrently_without_head_of_line_wait(self):
        persistent = Persistent()
        lock = threading.Lock()
        release = threading.Event()
        all_started = threading.Event()
        calls = []
        def poll():
            with lock:
                index = len(calls)
                calls.append(index)
                if len(calls) == 4:
                    all_started.set()
            if index == 0:
                release.wait(3)
            return {'id': str(index), 'lease': 'lease-' + str(index)}
        inbox = ConcurrentInbox(persistent, poll, capacity=4)
        try:
            inbox.next(4)
            self.assertTrue(all_started.wait(1), 'HTTP claims were serialized')
            deadline = time.monotonic() + 1
            seen = []
            while len(seen) < 3 and time.monotonic() < deadline:
                job = inbox.next(4)
                if job.get('id'):
                    seen.append(job['id'])
                time.sleep(.001)
            self.assertEqual(set(seen), {'1', '2', '3'})
            self.assertFalse(release.is_set())
        finally:
            release.set()

    def test_collectors_reserve_only_free_execution_slots(self):
        release = threading.Event()
        started = threading.Event()
        calls = []
        def poll():
            calls.append(True)
            started.set()
            release.wait(3)
            return {'idle': True}
        inbox = ConcurrentInbox(Persistent(), poll, capacity=8)
        try:
            inbox.next(2)
            self.assertTrue(started.wait(1))
            time.sleep(.03)
            self.assertEqual(len(calls), 2)
            for unused in range(20):
                inbox.next(2)
            self.assertEqual(len(calls), 2)
        finally:
            release.set()

    def test_socket_delivery_continues_while_entire_http_pool_blocks(self):
        persistent = Persistent()
        release = threading.Event()
        inbox = ConcurrentInbox(persistent, lambda: release.wait(3) or {'idle': True}, capacity=4)
        try:
            inbox.next(4)
            job = {'id': 'socket', 'lease': 'same'}
            persistent.jobs.put(job)
            self.assertIs(inbox.next(4), job)
        finally:
            release.set()

    def test_ready_later_claim_survives_an_earlier_claim_error(self):
        lock = threading.Lock()
        ready = threading.Event()
        calls = []
        def poll():
            with lock:
                index = len(calls)
                calls.append(index)
            if index == 0:
                raise IOError('test')
            ready.set()
            return {'id': 'ok', 'lease': 'original'}
        inbox = ConcurrentInbox(Persistent(), poll, capacity=2)
        inbox.next(2)
        self.assertTrue(ready.wait(1))
        with self.assertRaises(IOError):
            inbox.next(2)
        self.assertEqual(inbox.next(2), {'id': 'ok', 'lease': 'original'})

    def test_dispatch_deduplicates_same_active_job_and_lease(self):
        persistent = Persistent()
        stopped = threading.Event()
        release = threading.Event()
        started = threading.Event()
        seen = []
        def process(job):
            seen.append(job)
            started.set()
            release.wait(3)
            stopped.set()
        def poll():
            return {'id': 'same', 'lease': 'same-lease'}
        thread = threading.Thread(target=dispatch_loop, args=(
            persistent, poll, process, 4, lambda: True, lambda e: None, stopped))
        thread.start()
        try:
            self.assertTrue(started.wait(1))
            time.sleep(.05)
            self.assertEqual(len(seen), 1)
        finally:
            release.set()
            stopped.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())

if __name__ == '__main__':
    unittest.main()
