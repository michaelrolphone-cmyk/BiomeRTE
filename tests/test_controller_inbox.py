import os
import queue
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'deploy'))
from controller_inbox import ConcurrentInbox, dispatch_loop


class Persistent:
    def __init__(self):
        self.jobs = queue.Queue()
        self.socket = object()

    def next(self):
        try:
            return self.jobs.get(timeout=.01)
        except queue.Empty:
            return None


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.persistent = Persistent()
        self.release_poll = threading.Event()
        self.poll_started = threading.Event()
        self.poll_calls = 0
        self.http_job = {'id': 'http-job', 'lease': 'http-lease', 'request': {}}

    def tearDown(self):
        self.release_poll.set()

    def poll(self):
        self.poll_calls += 1
        self.poll_started.set()
        if not self.release_poll.wait(5):
            raise AssertionError('Test did not release HTTP poll')
        return self.http_job

    def test_blocked_http_does_not_delay_socket_or_overlap_polls(self):
        inbox = ConcurrentInbox(self.persistent, self.poll)
        self.assertEqual(inbox.next(), {'idle': True})
        self.assertTrue(self.poll_started.wait(1))
        socket = object()
        job = {'id': 'socket-job', 'lease': 'socket-lease', '_delivery_socket': socket}
        self.persistent.jobs.put(job)
        started = time.monotonic()
        self.assertIs(inbox.next(), job)
        self.assertLess(time.monotonic() - started, .2)
        self.assertFalse(self.release_poll.is_set())
        for unused in range(3):
            self.assertEqual(inbox.next(), {'idle': True})
        self.assertEqual(self.poll_calls, 1)
        pending = inbox.pending
        self.release_poll.set()
        self.assertIs(pending.result(1), self.http_job)
        self.assertIs(inbox.next(), self.http_job)
        self.assertIsNone(inbox.pending)
        self.assertNotIn('_delivery_socket', self.http_job)

    def test_ready_http_claim_is_not_starved_by_socket_arrivals(self):
        self.release_poll.set()
        inbox = ConcurrentInbox(self.persistent, self.poll)
        inbox.next()
        inbox.pending.result(1)
        job = {'id': 'socket-job', 'lease': 'socket-lease'}
        self.persistent.jobs.put(job)
        self.assertIs(inbox.next(), self.http_job)
        self.assertIs(inbox.next(), job)

    def test_http_error_is_observed_once_and_poll_can_recover(self):
        calls = []
        def poll():
            calls.append(True)
            if len(calls) == 1:
                raise IOError('Network unavailable')
            return self.http_job
        inbox = ConcurrentInbox(self.persistent, poll)
        inbox.next()
        with self.assertRaises(IOError):
            inbox.pending.result(1)
        with self.assertRaises(IOError):
            inbox.next()
        self.assertIsNone(inbox.pending)
        inbox.next()
        inbox.pending.result(1)
        self.assertIs(inbox.next(), self.http_job)
        self.assertEqual(len(calls), 2)

    def test_thread_start_failure_leaves_no_stuck_poll(self):
        inbox = ConcurrentInbox(self.persistent, self.poll)
        with patch('controller_inbox.threading.Thread.start', side_effect=RuntimeError('No thread')):
            with self.assertRaises(RuntimeError):
                inbox.next()
        self.assertIsNone(inbox.pending)

    def test_empty_http_response_preserves_waiting_socket(self):
        inbox = ConcurrentInbox(self.persistent, lambda: {'idle': True})
        inbox.next()
        inbox.pending.result(1)
        job = {'id': 'job', 'lease': 'lease'}
        self.persistent.jobs.put(job)
        self.assertIs(inbox.next(), job)

    def test_single_coordinator_caps_simultaneous_http_and_socket_execution(self):
        for capacity in (1, 2, 4):
            self._capacity_case(capacity)

    def _capacity_case(self, capacity):
        persistent = Persistent()
        stopped = threading.Event()
        release = threading.Event()
        poll_started = threading.Event()
        release_poll = threading.Event()
        lock = threading.Lock()
        state = {'active': 0, 'maximum': 0, 'seen': [], 'errors': []}
        def poll():
            poll_started.set()
            release_poll.wait(2)
            return {'id': 'http', 'lease': 'http-lease'}
        def process(job):
            with lock:
                state['active'] += 1
                state['maximum'] = max(state['maximum'], state['active'])
                state['seen'].append(job)
            release.wait(2)
            with lock:
                state['active'] -= 1
                if len(state['seen']) >= capacity + 1:
                    stopped.set()
        thread = threading.Thread(target=dispatch_loop,
            args=(persistent, poll, process, capacity, lambda: True,
                  state['errors'].append, stopped))
        thread.start()
        try:
            self.assertTrue(poll_started.wait(1))
            for index in range(capacity):
                persistent.jobs.put({'id': 'socket-' + str(index), 'lease': str(index),
                                     '_delivery_socket': persistent.socket})
            deadline = time.monotonic() + 1
            while len(state['seen']) < capacity and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(len(state['seen']), capacity)
            self.assertFalse(release_poll.is_set())
            release_poll.set()
            time.sleep(.15)
            self.assertEqual(len(state['seen']), capacity)
            release.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(state['maximum'], capacity)
            self.assertEqual([j['id'] for j in state['seen']].count('http'), 1)
            self.assertEqual(len(state['seen']), capacity + 1)
            self.assertEqual(state['errors'], [])
        finally:
            release_poll.set()
            release.set()
            stopped.set()
            thread.join(3)

    def test_not_ready_dispatches_no_job_and_starts_no_http_poll(self):
        stopped = threading.Event()
        calls = []
        self.persistent.jobs.put(self.http_job)
        thread = threading.Thread(target=dispatch_loop,
            args=(self.persistent, lambda: calls.append('poll'),
                  lambda job: calls.append('process'), 1, lambda: False,
                  calls.append, stopped))
        thread.start()
        time.sleep(.15)
        stopped.set()
        thread.join(1)
        self.assertEqual(calls, [])
        self.assertIs(self.persistent.jobs.get_nowait(), self.http_job)

    def test_http_error_backoff_keeps_socket_path_available(self):
        stopped = threading.Event()
        failed = threading.Event()
        seen = []
        calls = []
        def poll():
            calls.append('poll')
            raise IOError('Network unavailable')
        def error(value):
            failed.set()
        def process(job):
            seen.append(job)
            stopped.set()
        thread = threading.Thread(target=dispatch_loop,
            args=(self.persistent, poll, process, 1, lambda: True, error, stopped))
        thread.start()
        try:
            self.assertTrue(failed.wait(1))
            job = {'id': 'socket', 'lease': 'same', '_delivery_socket': self.persistent.socket}
            self.persistent.jobs.put(job)
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(seen, [job])
            self.assertEqual(calls, ['poll'])
        finally:
            stopped.set()
            thread.join(2)

    def test_capacity_is_validated_before_creating_threads(self):
        for capacity in (0, -1, True, '2'):
            with self.assertRaises(ValueError):
                dispatch_loop(self.persistent, self.poll, lambda job: None,
                              capacity, lambda: True, lambda error: None)


if __name__ == '__main__':
    unittest.main()
