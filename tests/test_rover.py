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

    async def test_fencing_polls_same_lease_and_disconnect_releases_before_retry(self):
        from unittest.mock import patch
        calls = []
        reached = asyncio.Event()
        class Remote:
            def call(self, operation, args):
                calls.append((operation, args))
                if operation == 'cap.acquire':
                    return {'id': 'held', 'state': 'fencing'}
                if operation == 'cap.lease':
                    return {'id': 'held', 'state': 'bound'}
                if operation == 'stream.list':
                    return []
                if operation == 'stream.open':
                    return {'id': 'session', 'lease': 'held', 'lanes': ['vision']}
                return {}
        rover = Rover(Remote(), {})
        async def disconnected(*args):
            rover.closed = True
            reached.set()
            raise ConnectionError('provider disconnected')
        rover.lane = disconnected
        original_sleep = asyncio.sleep
        async def yield_now(*args):
            await original_sleep(0)
        with patch('biomerte.rover.asyncio.sleep', yield_now):
            await rover.owner('vision:1', 'camera')
        self.assertTrue(reached.is_set())
        names = [name for name, args in calls]
        self.assertEqual(1, names.count('cap.acquire'))
        self.assertEqual({'lease': 'held'}, calls[names.index('cap.lease')][1])
        self.assertEqual(['stream.close', 'cap.release'], names[-2:])
        self.assertFalse(rover.descriptors)

    async def test_shutdown_during_fencing_releases_pending_lease(self):
        acquired = asyncio.Event()
        calls = []
        class Remote:
            def call(self, operation, args):
                calls.append((operation, args))
                if operation == 'cap.acquire':
                    return {'id': 'pending', 'state': 'fencing'}
                return {}
        rover = Rover(Remote(), {})
        original_call = rover.call
        async def call(operation, **args):
            result = await original_call(operation, **args)
            if operation == 'cap.acquire':
                acquired.set()
            return result
        rover.call = call
        task = asyncio.create_task(rover.owner('vision:1', 'camera'))
        await acquired.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(('cap.release', {'lease': 'pending'}), calls[-1])
