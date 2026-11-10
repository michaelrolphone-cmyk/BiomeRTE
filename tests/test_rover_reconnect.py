import asyncio,unittest
from biomerte.rover import Rover
from biomerte.store import Fault

class Remote:
    def __init__(self):self.calls=[];self.invalid=False
    def call(self,operation,args):
        self.calls.append((operation,args))
        if operation=='cap.acquire':return {'id':'lease','state':'bound'}
        if operation=='stream.list':return []
        if operation in ('stream.open','stream.attach'):
            if self.invalid:raise Fault('Superseded',409)
            return {'id':'session','lease':'lease','epoch':'epoch','lanes':['vision','map']}
        return {'state':'released'}

class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_failed_lane_keeps_lease_epoch_and_other_lane(self):
        remote=Remote();started=set();failed=[False];release=asyncio.Event()
        class Fixture(Rover):
            async def lane(self,descriptor,lane):
                started.add(lane)
                if lane=='map' and not failed[0]:
                    failed[0]=True;raise ConnectionError('Link interrupted')
                await release.wait()
        rover=Fixture(remote,{'vision:1':'camera'})
        await rover.start()
        try:
            for _ in range(100):
                if len([c for c in remote.calls if c[0]=='stream.attach'])>=3:break
                await asyncio.sleep(.02)
            self.assertEqual({'vision','map'},started)
            self.assertEqual(1,len([c for c in remote.calls if c[0]=='cap.acquire']))
            self.assertFalse(any(c[0]=='cap.release' for c in remote.calls))
            self.assertEqual('epoch',rover.descriptors['vision:1']['epoch'])
        finally:await rover.close()
        self.assertEqual(1,len([c for c in remote.calls if c[0]=='cap.release']))

    async def test_authoritative_rejection_exits_lane_instead_of_retrying_forever(self):
        remote=Remote();remote.invalid=True
        rover=Rover(remote,{})
        with self.assertRaises(Fault):
            await rover.maintain_lane({'id':'session'},'control')
        self.assertEqual(1,len(remote.calls))

if __name__=='__main__':unittest.main()
