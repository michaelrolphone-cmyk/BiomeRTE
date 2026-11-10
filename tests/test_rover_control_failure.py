import asyncio,json,unittest
from unittest.mock import patch
from biomerte.rover import Rover

class Socket:
    async def send(self,value):raise ConnectionError('sender failed while receiver stayed connected')
    def __aiter__(self):return self
    async def __anext__(self):await asyncio.Event().wait()
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass

class ControlFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_sender_failure_clears_readiness_and_pending_intent(self):
        rover=Rover(None,{})
        descriptor=dict(url='wss://router/session',attachment_credential='credential',authority='owner',generation=1,epoch='epoch')
        with patch('websockets.asyncio.client.connect',return_value=Socket()):
            lane=asyncio.create_task(rover.lane(descriptor,'control'))
            for _ in range(20):
                if rover.control_ready:break
                await asyncio.sleep(0)
            self.assertTrue(rover.control_ready)
            rover.desired(0)
            with self.assertRaises(ConnectionError):await asyncio.wait_for(lane,.5)
            self.assertFalse(rover.control_ready)
            self.assertTrue(rover.intent.empty())

    async def test_map_overflow_retains_pinned_snapshot_target(self):
        rover=Rover(None,{})
        async with rover.subscribe() as queues:
            for revision in range(9):
                rover.broadcast('map',json.dumps(dict(type='map_delta',map_id='map',revision=revision)))
            value=json.loads(queues['map'].get_nowait())
            self.assertEqual({'type':'snapshot_required','map_id':'map','revision':8},value)
