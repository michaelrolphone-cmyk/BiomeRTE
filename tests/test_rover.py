import asyncio
import unittest
from biomerte.rover import Rover


class RoverOwnership(unittest.IsolatedAsyncioTestCase):
    async def test_browser_attach_detach_never_calls_controller_or_replays_intent(self):
        class Remote:
            def call(self,*args):raise AssertionError('Browser must not acquire or release resources')
        rover=Rover(Remote(),{})
        async with rover.subscribe() as first:
            async with rover.subscribe() as second:
                rover.broadcast('vision',b'fresh')
                self.assertEqual(b'fresh',await first['vision'].get())
                self.assertEqual(b'fresh',await second['vision'].get())
        self.assertFalse(rover.browsers)
        self.assertTrue(rover.intent.empty())

    async def test_latest_fresh_desired_state_wins_and_stop_is_immediate(self):
        rover=Rover(None,{})
        rover.control_ready=True
        rover.desired(1)
        rover.desired(2)
        self.assertEqual(1,rover.intent.qsize())
        self.assertEqual(2,(await rover.intent.get())['outputs']['mask'])
        rover.desired(1)
        rover.stop()
        self.assertEqual(0,(await rover.intent.get())['outputs']['mask'])
        rover.desired(1)
        rover.discard_intent()
        self.assertTrue(rover.intent.empty())

    async def test_slow_browser_video_does_not_backpressure_control(self):
        rover=Rover(None,{})
        async with rover.subscribe() as queues:
            for i in range(10000):rover.broadcast('vision',i)
            rover.broadcast('control',{'stage':'applied'})
            self.assertEqual(9999,await queues['vision'].get())
            self.assertEqual('applied',(await queues['control'].get())['stage'])

    async def test_map_revision_overflow_requests_snapshot(self):
        rover=Rover(None,{})
        async with rover.subscribe() as queues:
            for i in range(9):rover.broadcast('map',str(i))
            self.assertEqual('{"type": "snapshot_required"}',await queues['map'].get())
