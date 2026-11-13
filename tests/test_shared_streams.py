"""Generic sharing qualification using an arbitrary capability and lane."""
import asyncio, json, unittest
from tests.test_stream_grants import StreamGrants
from tests.test_stream_wire_integration import StreamWire, connect
from biomerte.stream_router import decode, frame

class SharedGrants(StreamGrants):
    def start_shared(self):
        self.offer('persistent',stream={'enabled':True,'lanes':['arbitrary.events'],
                                       'direction':'bidirectional'})
        first=self.acquire('first')
        self.call('cap.fenced',provider='test',lease=first['id'],generation=first['generation'])
        stream=self.call('stream.open',lease=first['id'])
        second=self.acquire('second')
        return first,second,stream

    def test_one_reservation_two_independent_subscriptions(self):
        first,second,stream=self.start_shared()
        self.assertEqual('bound',second['state'])
        self.assertEqual(first['id'],second['shared_parent'])
        self.assertEqual(first['generation'],second['generation'])
        shared=self.call('stream.open',lease=second['id'])
        self.assertEqual(stream['id'],shared['id'])
        self.assertEqual(stream['epoch'],shared['epoch'])
        self.call('cap.release',lease=first['id'])
        self.assertEqual('open',self.call('stream.inspect',session=stream['id'])['state'])
        self.assertEqual([second['id']],self.call('stream.inspect',session=stream['id'])['consumers'])
        self.call('cap.release',lease=second['id'])
        with self.assertRaises(Exception):
            self.call('stream.inspect',session=stream['id'])

    def test_anchor_owner_reacquires_after_release_with_other_reader(self):
        first,second,stream=self.start_shared()
        self.call('cap.release',lease=first['id'])
        self.assertEqual([second['id']],self.call('stream.inspect',session=stream['id'])['consumers'])
        rejoined=self.acquire('first')
        self.assertFalse(rejoined.get('shared_owner_released'))
        self.assertEqual(first['id'],rejoined['id'])
        self.assertEqual(first['generation'],rejoined['generation'])
        reopened=self.call('stream.open',lease=rejoined['id'])
        self.assertEqual(stream['id'],reopened['id'])
        self.assertEqual({first['id'],second['id']},set(self.call('stream.inspect',session=stream['id'])['consumers']))
        self.assertEqual('bound',self.call('cap.lease',lease=second['id'])['state'])

    def test_anchor_owner_rejoins_after_explicit_stream_close(self):
        first,second,stream=self.start_shared()
        self.call('stream.close',session=stream['id'],lease=first['id'])
        self.assertEqual([second['id']],self.call('stream.inspect',session=stream['id'])['consumers'])
        with self.assertRaises(Exception):
            self.call('stream.open',lease=first['id'])
        self.acquire('first')
        self.assertEqual(stream['id'],self.call('stream.open',lease=first['id'])['id'])
        self.assertEqual({first['id'],second['id']},set(self.call('stream.inspect',session=stream['id'])['consumers']))

    def test_discovery_publishes_generic_shared_stream_path(self):
        self.start_shared()
        discovered=self.call('cap.discover',contract='sample:1')['offers'][0]['usage']
        self.assertEqual('simultaneous_consumers_same_endpoint',discovered['sharing'])
        operations=[c['operation'] for c in discovered['calls']]
        self.assertIn('stream.open',operations)
        self.assertNotIn('cap.invoke',operations)

    def test_subscriber_release_does_not_release_original(self):
        first,second,stream=self.start_shared()
        self.call('cap.release',lease=second['id'])
        self.assertEqual('bound',self.call('cap.lease',lease=first['id'])['state'])
        self.assertEqual([first['id']],self.call('stream.inspect',session=stream['id'])['consumers'])

    def test_recovery_does_not_treat_subscriber_as_fence_holder(self):
        first,second,stream=self.start_shared()
        self.app.tick('site')
        self.assertEqual('bound',self.call('cap.lease',lease=second['id'])['state'])
        self.assertEqual('open',self.call('stream.inspect',session=stream['id'])['state'])

    def test_close_one_subscription_keeps_other_endpoint_open(self):
        first,second,stream=self.start_shared()
        self.call('stream.close',session=stream['id'],lease=second['id'])
        self.assertEqual('open',self.call('stream.inspect',session=stream['id'])['state'])
        self.assertEqual('bound',self.call('cap.lease',lease=first['id'])['state'])
        self.assertEqual('released',self.call('cap.lease',lease=second['id'])['state'])

    def test_independent_identity_attaches_only_with_owned_subscription(self):
        from types import SimpleNamespace
        from biomerte import streams
        first,second,stream=self.start_shared()
        def check(tx):
            lease=tx.need('leases',second['id']);lease['owner']='independent-client'
            tx.put('leases',lease['id'],lease)
            ctx=SimpleNamespace(app=self.app,permissions=[],actor='independent-client',scope='site')
            attached=streams.attach(tx,ctx,stream['id'],'server',second['id'])
            self.assertTrue(attached['attachment_credential'])
            with self.assertRaises(Exception):
                streams.attach(tx,ctx,stream['id'],'server',first['id'])
            ctx.actor='unsubscribed-client'
            with self.assertRaises(Exception):
                streams.attach(tx,ctx,stream['id'],'server')
        self.app.stores['site'].execute(check)

@unittest.skipIf(connect is None,'websockets transport unavailable')
class SharedWire(StreamWire):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.grant['lanes'].append('arbitrary.events')
        self.grant['delivery']='ordered'
        self.grant['limits']['buffer_depth']=8

    def message(self,sequence,**extra):
        return json.dumps(dict(authority=self.grant['authority'],generation=3,
                               epoch=self.grant['epoch'],sequence=sequence,**extra))

    async def test_two_consumers_same_endpoint_receive_binary_and_survive_disconnect(self):
        provider=await self.attach('provider','arbitrary.events')
        one=await self.attach('server','arbitrary.events')
        two=await self.attach('server','arbitrary.events')
        await asyncio.sleep(.02)
        original=frame(json.loads(self.message(1)),b'generic-payload')
        await provider.send(original)
        for client in (one,two):
            meta,payload=decode(await asyncio.wait_for(client.recv(),2),4096)
            self.assertEqual(b'generic-payload',payload)
        self.assertIsNone(one.close_code)
        self.assertIsNone(two.close_code)
        await one.close()
        await provider.send(self.message(2,value='still-open'))
        self.assertEqual('still-open',json.loads(await asyncio.wait_for(two.recv(),2))['value'])
        await two.close();await provider.close()

    async def test_independent_producers_start_at_same_sequence(self):
        provider=await self.attach('provider','arbitrary.events')
        one=await self.attach('server','arbitrary.events')
        two=await self.attach('server','arbitrary.events')
        await asyncio.sleep(.02)
        await one.send(self.message(1,value='one'))
        first=json.loads(await asyncio.wait_for(provider.recv(),2))
        await two.send(self.message(1,value='two'))
        second=json.loads(await asyncio.wait_for(provider.recv(),2))
        self.assertEqual(['one','two'],[first['value'],second['value']])
        self.assertGreater(second['sequence'],first['sequence'])
        self.assertEqual(1,second['source_sequence'])
        self.assertNotEqual(first['source_attachment'],second['source_attachment'])
        self.assertIsNone(one.close_code)
        await one.close();await two.close();await provider.close()


    async def test_release_revokes_only_its_connections(self):
        self.grant['consumers']=['first','second']
        provider=await self.attach('provider','arbitrary.events')
        self.grant['consumer']='first'
        one=await self.attach('server','arbitrary.events')
        self.grant['consumer']='second'
        two=await self.attach('server','arbitrary.events')
        await asyncio.sleep(.02)
        self.grant['consumers']=['second']
        await asyncio.wait_for(one.wait_closed(),3)
        self.assertIsNone(two.close_code)
        await provider.send(self.message(1,value='survivor'))
        self.assertEqual('survivor',json.loads(await asyncio.wait_for(two.recv(),2))['value'])
        await two.close();await provider.close()
