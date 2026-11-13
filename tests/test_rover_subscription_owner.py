import asyncio
import unittest
from biomerte.rover import Rover


class OwnerSubscription(unittest.IsolatedAsyncioTestCase):
    async def test_startup_reuses_session_and_shutdown_releases_only_own_subscription(self):
        calls=[]
        class Remote:
            def call(self,op,args):
                calls.append((op,args))
                if op=='cap.acquire':return {'id':'own','state':'bound'}
                if op=='stream.open':return {'id':'shared','lease':'parent','lanes':['vision']}
                if op in ('stream.close','cap.release'):return {}
                raise AssertionError('Unexpected ownership call '+op)
        rover=Rover(Remote(),{'robot.vision.stream:1':'camera'})
        ready=asyncio.Event()
        async def lane(*args):ready.set();await asyncio.Future()
        rover.maintain_lane=lane
        await rover.start();await asyncio.wait_for(ready.wait(),2)
        self.assertEqual('own',rover.descriptors['robot.vision.stream:1']['subscription_lease'])
        self.assertFalse(any(op=='stream.close' for op,args in calls))
        await rover.close()
        closed=[args for op,args in calls if op=='stream.close']
        self.assertTrue(closed)
        self.assertTrue(all(args['lease']=='own' for args in closed))
        self.assertTrue(all(args['lease']=='own' for op,args in calls if op=='cap.release'))
