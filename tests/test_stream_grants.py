from tests.test_bounded_lifecycle import BoundedLifecycle
from biomerte.app import Application
from biomerte.store import Fault


class StreamGrants(BoundedLifecycle):
    def setUp(self):
        super().setUp()
        self.app.config.update(stream_router_secret='a'*64, stream_gateway_url='wss://gateway/api/stream')

    def test_open_holds_lease_and_messages_need_no_actions(self):
        self.offer('persistent', stream={'enabled':True,'lanes':['vision'],'direction':'provider_to_server'})
        lease=self.acquire()
        self.call('cap.fenced',provider='test',lease=lease['id'],generation=lease['generation'])
        stream=self.call('stream.open',lease=lease['id'])
        self.assertEqual('bound',self.call('cap.lease',lease=lease['id'])['state'])
        self.assertEqual('vision',stream['lanes'][0])
        self.assertTrue(self.call('stream.attach',session=stream['id'],role='provider')['attachment_credential'])
        self.assertEqual(stream['id'], self.call('stream.open',lease=lease['id'])['id'])
        self.call('cap.release',lease=lease['id'])
        with self.assertRaises(Fault):
            self.call('stream.inspect',session=stream['id'])

    def test_explicit_renewal_preserves_the_stream_epoch(self):
        from unittest.mock import patch
        self.offer('persistent', stream={'enabled': True, 'lanes': ['vision', 'map']})
        lease = self.acquire()
        self.call('cap.fenced', provider='test', lease=lease['id'], generation=lease['generation'])
        with patch('biomerte.streams.time.time', return_value=1000):
            stream = self.call('stream.open', lease=lease['id'], ttl_seconds=60)
        with patch('biomerte.streams.time.time', return_value=1040):
            self.assertEqual(1060, self.call('stream.open', lease=lease['id'])['expires_at'])
            renewed = self.call('stream.open', lease=lease['id'], renew=True, ttl_seconds=60)
        for key in ('id', 'epoch', 'lease', 'generation'):
            self.assertEqual(stream[key], renewed[key])
        self.assertEqual(1100, renewed['expires_at'])
        with patch('biomerte.streams.time.time', return_value=1070):
            self.assertEqual(stream['id'], self.call('stream.inspect', session=stream['id'])['id'])

    def test_command_lease_cannot_open_stream(self):
        lease, action=self.action()
        with self.assertRaises(Fault):
            self.call('stream.open',lease=lease['id'])

    def test_control_and_vision_use_separate_grants_and_revoke_is_terminal(self):
        self.offer('persistent',stream={'enabled':True,'lanes':['control'],'direction':'bidirectional'})
        lease=self.acquire()
        self.call('cap.fenced',provider='test',lease=lease['id'],generation=lease['generation'])
        stream=self.call('stream.open',lease=lease['id'])
        self.call('stream.revoke',session=stream['id'])
        with self.assertRaises(Fault):
            self.call('stream.attach',session=stream['id'],role='server')

    def test_provider_restart_only_receives_grant_for_current_fence(self):
        self.offer('persistent', stream={'enabled': True})
        old_lease = self.acquire()
        self.call('cap.fenced', provider='test', lease=old_lease['id'], generation=old_lease['generation'])
        old_stream = self.call('stream.open', lease=old_lease['id'])
        self.call('cap.offer', provider='test', contract='sample:1', resource='slot', state='offline')
        self.app.tick('site')
        self.assertEqual('released', self.call('cap.lease', lease=old_lease['id'])['state'])

        self.app = Application(self.tmp.name)
        self.app.config.update(stream_router_secret='a'*64, stream_gateway_url='wss://gateway/api/stream')
        self.offer('persistent', stream={'enabled': True})
        new_lease = self.acquire()
        self.assertEqual(old_lease['id'], new_lease['id'])
        self.assertGreater(new_lease['generation'], old_lease['generation'])
        with self.assertRaises(Fault) as stale_fence:
            self.call('cap.fenced', provider='test', lease=old_lease['id'], generation=old_lease['generation'])
        self.assertEqual(409, stale_fence.exception.status)

        self.assertEqual([new_lease['id']], [lease['id'] for lease in self.call('provider.poll', provider='test')['fences']])
        self.call('cap.fenced', provider='test', lease=new_lease['id'], generation=new_lease['generation'])
        self.assertEqual([], self.call('provider.poll', provider='test')['streams'])
        with self.assertRaises(Fault) as stale_stream:
            self.call('stream.attach', session=old_stream['id'], role='provider')
        self.assertEqual(409, stale_stream.exception.status)

        new_stream = self.call('stream.open', lease=new_lease['id'])
        self.assertNotEqual(old_stream['id'], new_stream['id'])
        self.assertEqual([new_stream['id']], [grant['id'] for grant in self.call('provider.poll', provider='test')['streams']])
        self.assertTrue(self.call('stream.attach', session=new_stream['id'], role='provider')['attachment_credential'])

    def test_released_lease_collection_does_not_block_another_stream(self):
        self.offer('persistent', stream={'enabled': True})
        released = self.acquire('released')
        self.call('cap.fenced', provider='test', lease=released['id'], generation=released['generation'])
        self.call('stream.open', lease=released['id'])
        self.call('cap.release', lease=released['id'])
        self.now += 301
        self.app.tick('site')
        with self.assertRaises(Fault) as missing:
            self.call('cap.lease', lease=released['id'])
        self.assertEqual(404, missing.exception.status)

        current = self.acquire('current')
        self.call('cap.fenced', provider='test', lease=current['id'], generation=current['generation'])
        live_stream = self.call('stream.open', lease=current['id'])
        inbox = self.call('provider.poll', provider='test')
        self.assertEqual([live_stream['id']], [grant['id'] for grant in inbox['streams']])
