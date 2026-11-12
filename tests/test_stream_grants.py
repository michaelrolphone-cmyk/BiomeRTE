from tests.test_bounded_lifecycle import BoundedLifecycle
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
