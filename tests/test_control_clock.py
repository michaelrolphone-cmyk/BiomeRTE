import asyncio
import struct
import unittest
from unittest.mock import patch

from biomerte.control_clock import ControlClock, NTP_EPOCH, consensus, ntp_sample
from biomerte.rover import Rover
from biomerte.stream_router import Session


class ClockTests(unittest.TestCase):
    def clock(self):
        now = [1000.0]
        clock = ControlClock(monotonic=lambda: now[0])
        clock.interval, clock.sampled_at = (1699999000.0, 1699999000.04), now[0]
        return clock, now

    def test_wall_clock_skew_no_longer_consumes_the_hold_deadline(self):
        clock, now = self.clock()
        # True UTC is inside the interval; the host wall clock is 160ms slow.
        true_utc = clock.lower() + .02
        with patch('time.time', return_value=true_utc-.160):
            old = Rover(None, {})
            old.control_ready = True
            old.desired(1, 500)
            old_frame = old.intent.get_nowait()
            rover = Rover(None, {}, clock=clock)
            rover.control_ready = True
            rover.desired(1, 500)
            frame = rover.intent.get_nowait()
        arrival = true_utc + .096
        # The observed 279ms refresh gap exceeded the old remaining lifetime.
        self.assertLess(old_frame['expires_at']-arrival, .279)
        self.assertGreater(frame['expires_at']-arrival, .279)
        self.assertAlmostEqual(frame['expires_at']-frame['issued_at'], .5, places=6)

    def test_router_uses_utc_bound_without_loosening_future_or_expiry_checks(self):
        clock, now = self.clock()
        grant = dict(authority='a'*64, generation=1, epoch='b'*32, lanes=['control'],
                     direction='bidirectional', limits={'max_message_bytes':2048})
        rover = Rover(None, {}, clock=clock)
        rover.control_ready = True
        rover.desired(1, 500)
        frame = rover.intent.get_nowait()
        frame.update(authority=grant['authority'], generation=1, epoch=grant['epoch'])
        import json
        session = Session(grant, control_clock=clock)
        with patch('time.time', return_value=clock.lower()-.160):
            session.check('server', 'control', json.dumps(frame))
        now[0] += .501
        frame['sequence'] += 1
        with self.assertRaisesRegex(Exception, 'Expired'):
            session.check('server', 'control', json.dumps(frame))
        frame.update(sequence=3, issued_at=clock.upper()+.01, expires_at=clock.upper()+.51)
        with self.assertRaisesRegex(Exception, 'Expired'):
            session.check('server', 'control', json.dumps(frame))

    def test_stale_clock_stops_new_motion_and_refresh_does_not_replay(self):
        clock, now = self.clock()
        rover = Rover(None, {}, clock=clock)
        rover.control_ready = True
        now[0] += 91
        self.assertFalse(rover.control_ready)
        with self.assertRaises(RuntimeError):
            rover.desired(1, 500)
        clock.sampled_at = now[0]
        self.assertTrue(rover.control_ready)
        self.assertTrue(rover.intent.empty())

    def test_monotonic_clock_survives_wall_time_jump(self):
        clock, now = self.clock()
        before = clock.lower()
        now[0] += .1
        with patch('time.time', return_value=0):
            self.assertAlmostEqual(clock.lower()-before, .1-.000005, places=6)

    def test_consensus_requires_agreement_and_rejects_one_outlier(self):
        self.assertEqual(consensus([(1, 1.04), (1.01, 1.05), (20, 20.01)]), (1.01, 1.04))
        with self.assertRaises(RuntimeError):
            consensus([(1, 1.01), (2, 2.01), OSError('offline')])

    def test_ntp_sample_validates_origin_and_accounts_for_round_trip(self):
        class Wire:
            valid = True
            def __enter__(self): return self
            def __exit__(self, *unused): pass
            def settimeout(self, unused): pass
            def connect(self, unused): pass
            def send(self, request): self.request = bytes(request)
            def recv(self, unused):
                reply = bytearray(48)
                reply[0], reply[1] = 0x24, 1
                reply[24:32] = self.request[40:48] if self.valid else bytes(8)
                for index in (32, 40):
                    struct.pack_into('!II', reply, index, NTP_EPOCH+1700000000, 0)
                return reply
        wire = Wire()
        with patch('socket.socket', return_value=wire), patch('time.monotonic', side_effect=[1000,1000.04]):
            lower, upper = ntp_sample('test')
        self.assertAlmostEqual(lower+1000.02, 1700000000-.025, places=5)
        self.assertAlmostEqual(upper+1000.02, 1700000000+.025, places=5)
        wire.valid = False
        with patch('socket.socket', return_value=wire), patch('time.monotonic', side_effect=[1000,1000.04]):
            with self.assertRaises(ValueError): ntp_sample('test')


class AsyncClockTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_network_sampling_does_not_block_control_event_loop(self):
        import threading
        release = threading.Event()
        def sample(unused):
            release.wait(2)
            return (1, 1.01)
        clock = ControlClock(sampler=sample)
        task = asyncio.create_task(clock.start())
        try:
            await asyncio.wait_for(asyncio.sleep(.01), .1)
            self.assertFalse(task.done())
        finally:
            release.set()
            await task
            await clock.close()


if __name__ == '__main__':
    unittest.main()
