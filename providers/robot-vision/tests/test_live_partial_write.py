"""A slow reader can pause mid-frame without resetting other live subscribers."""
import pathlib
import socket
import sys
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from vision_service import write_live_frame


class PartialWriteTests(unittest.TestCase):
    def test_backpressure_resumes_after_the_last_written_byte(self):
        closed = threading.Event()
        output = bytearray()

        class Peer:
            calls = 0
            def send(self, data):
                self.calls += 1
                if self.calls == 2:
                    raise socket.timeout()
                size = min(len(data), 3)
                output.extend(data[:size])
                return size

        self.assertTrue(write_live_frame(Peer(), b'123456789', threading.Event(), closed))
        self.assertEqual(output, b'123456789')

    def test_lease_shutdown_interrupts_backpressure(self):
        stop = threading.Event()

        class Peer:
            def send(self, data):
                stop.set()
                raise socket.timeout()

        self.assertFalse(write_live_frame(Peer(), b'12345', stop, threading.Event()))
