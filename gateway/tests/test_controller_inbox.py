"""Exercise real dispatcher threads without network or controller mutations."""
import os
import queue
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'deploy'))
from controller_inbox import dispatch_loop


class Link:
    def __init__(self, connected=True):
        self.jobs = queue.Queue()
        self.socket = object() if connected else None
        self.http_poll_interval = 2


class DispatchWakeTests(unittest.TestCase):
    def start(self, link, poll, process, capacity=8):
        stopped = threading.Event()
        errors = []
        thread = threading.Thread(target=dispatch_loop,
                                  args=(link, poll, process, capacity,
                                        lambda: True, errors.append, stopped))
        thread.start()
        self.addCleanup(self.finish, stopped, thread, link)
        return stopped, errors

    def finish(self, stopped, thread, link):
        stopped.set()
        link.jobs.put({'idle': True})
        thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_http_response_wakes_delivery_without_idle_sleep(self):
        link = Link(False)
        polling, release, delivered = (threading.Event() for unused in range(3))
        self.addCleanup(release.set)
        def poll():
            polling.set()
            release.wait()
            return {'id': 'http-job'}
        stopped, errors = self.start(link, poll, lambda job: delivered.set())
        self.assertTrue(polling.wait(1))
        started = time.monotonic()
        release.set()
        self.assertTrue(delivered.wait(.2))
        print('http_fallback_dispatch_ms=%.3f' % ((time.monotonic()-started)*1000))
        self.assertEqual(errors, [])

    def test_slow_http_recovery_does_not_block_socket_delivery(self):
        link = Link()
        polling, release, delivered = (threading.Event() for unused in range(3))
        self.addCleanup(release.set)
        def poll():
            polling.set()
            release.wait()
            return {'idle': True}
        stopped, errors = self.start(link, poll, lambda job: delivered.set())
        self.assertTrue(polling.wait(1))
        started = time.monotonic()
        link.jobs.put({'id': 'socket-job', 'lease': 'original-lease'})
        self.assertTrue(delivered.wait(.2))
        print('socket_dispatch_ms=%.3f' % ((time.monotonic()-started)*1000))
        self.assertEqual(errors, [])

    def test_free_slot_wakes_queued_work(self):
        link = Link()
        first, release, second = (threading.Event() for unused in range(3))
        self.addCleanup(release.set)
        seen = []
        def process(job):
            seen.append(job['id'])
            if job['id'] == 'first':
                first.set()
                release.wait()
            else:
                second.set()
        stopped, errors = self.start(link, lambda: {'idle': True}, process, 1)
        link.jobs.put({'id': 'first'})
        self.assertTrue(first.wait(1))
        link.jobs.put({'id': 'second'})
        self.assertFalse(second.is_set())
        started = time.monotonic()
        release.set()
        self.assertTrue(second.wait(.2))
        print('capacity_wake_dispatch_ms=%.3f' % ((time.monotonic()-started)*1000))
        self.assertEqual(seen, ['first', 'second'])
        self.assertEqual(errors, [])

    def test_http_error_does_not_block_socket_delivery(self):
        link, failed, delivered = Link(), threading.Event(), threading.Event()
        def poll():
            failed.set()
            raise OSError('test recovery failure')
        stopped, errors = self.start(link, poll, lambda job: delivered.set())
        self.assertTrue(failed.wait(1))
        link.jobs.put({'id': 'socket-job'})
        self.assertTrue(delivered.wait(.2))


if __name__ == '__main__':
    unittest.main()
