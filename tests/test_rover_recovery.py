"""Authoritative recovery for stream RPC errors that lose their HTTP status."""
import asyncio
import types
import unittest
from unittest import mock
from biomerte.rover import Rover


class StreamRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def descriptor(self):
        return dict(id='session', lease='lease', generation=3, lanes=['vision'])

    async def test_missing_grant_exits_retry_even_without_http_status(self):
        rover = Rover(None, {})
        async def call(operation, **args):
            if operation == 'stream.attach':
                raise RuntimeError('Unknown _stream_grants session')
            if operation == 'cap.lease':
                return dict(state='bound', generation=3)
            if operation == 'stream.list':
                return []
            self.fail(operation)
        rover.call = call
        with self.assertRaisesRegex(ConnectionError, 'no longer current'):
            await asyncio.wait_for(rover.maintain_lane(self.descriptor(), 'vision'), .2)

    async def test_superseded_lease_exits_even_if_grant_is_still_listed(self):
        rover = Rover(None, {})
        async def call(operation, **args):
            if operation == 'stream.attach':
                raise RuntimeError('Stream authority superseded')
            if operation == 'cap.lease':
                return dict(state='bound', generation=4)
            if operation == 'stream.list':
                return [self.descriptor()]
            self.fail(operation)
        rover.call = call
        with self.assertRaisesRegex(ConnectionError, 'no longer current'):
            await asyncio.wait_for(rover.maintain_lane(self.descriptor(), 'vision'), .2)

    async def retry_case(self, authority_reachable):
        rover = Rover(None, {})
        calls = []
        async def call(operation, **args):
            calls.append((operation, args))
            if operation == 'stream.attach':
                if sum(op == operation for op, _ in calls) == 1:
                    raise ConnectionError('network unavailable')
                return self.descriptor()
            if operation == 'cap.lease':
                if not authority_reachable:
                    raise ConnectionError('authority unavailable')
                return dict(state='bound', generation=3)
            if operation == 'stream.list':
                return [self.descriptor()]
            self.fail(operation)
        async def lane(descriptor, lane):
            self.assertEqual(descriptor['id'], 'session')
            rover.closed = True
        rover.call, rover.lane = call, lane
        with mock.patch('biomerte.rover.asyncio.sleep', new=mock.AsyncMock()):
            await rover.maintain_lane(self.descriptor(), 'vision')
        self.assertEqual(sum(op == 'stream.attach' for op, _ in calls), 2)
        self.assertFalse(any(op in ('cap.acquire', 'cap.release', 'stream.close') for op, _ in calls))
        self.assertTrue(all(args.get('session', 'session') == 'session' for _, args in calls))

    async def test_temporary_transport_failure_retains_current_session(self):
        await self.retry_case(True)

    async def test_unreachable_authority_does_not_prove_session_loss(self):
        await self.retry_case(False)

    async def test_nested_websocket_rejection_returns_to_owner(self):
        rover = Rover(None, {})
        async def call(operation, **args):
            self.assertEqual(operation, 'stream.attach')
            return self.descriptor()
        async def lane(descriptor, lane):
            error = ConnectionError('handshake rejected')
            error.response = types.SimpleNamespace(status_code=409)
            raise error
        rover.call, rover.lane = call, lane
        with self.assertRaisesRegex(ConnectionError, 'handshake rejected'):
            await asyncio.wait_for(rover.maintain_lane(self.descriptor(), 'vision'), .2)

    async def test_owner_reacquires_after_deleted_grant_without_replaying_intent(self):
        rover = Rover(None, {})
        events = []
        generation = 0
        async def call(operation, **args):
            nonlocal generation
            events.append((operation, args))
            if operation == 'cap.acquire':
                generation += 1
                return dict(id='lease'+str(generation), state='bound', generation=generation)
            if operation == 'cap.lease':
                return dict(state='bound', generation=generation)
            if operation == 'stream.list':
                return []
            if operation == 'stream.open':
                return dict(id='s'+str(generation), lease='lease'+str(generation), generation=generation, lanes=['vision'])
            if operation == 'stream.attach':
                if args['session'] == 's1':
                    raise RuntimeError('Unknown _stream_grants s1')
                return dict(id='s2')
            if operation in ('stream.close', 'cap.release'):
                return {}
            self.fail(operation)
        async def lane(descriptor, lane):
            self.assertEqual(descriptor['id'], 's2')
            self.assertTrue(rover.intent.empty())
            rover.closed = True
        rover.call, rover.lane = call, lane
        with mock.patch('biomerte.rover.asyncio.sleep', new=mock.AsyncMock()):
            await rover.owner('robot.vision.stream:1', 'camera')
        self.assertEqual(generation, 2)
        self.assertIn(('cap.release', {'lease':'lease1'}), events)
        self.assertEqual([args['session'] for op, args in events if op == 'stream.attach'], ['s1', 's2'])


if __name__ == '__main__':
    unittest.main()
