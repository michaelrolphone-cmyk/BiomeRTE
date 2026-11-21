from tests.test_bounded_lifecycle import BoundedLifecycle
from biomerte.api import Context
from biomerte.service_authority import ScopedAuthority
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

    def test_default_control_stream_survives_original_expiry(self):
        contract = 'device.outputs.stream:1'
        self.call('cap.define', name='device.outputs.stream', inputs={}, outputs={})

        def open_control(resource, **lifetime):
            self.call('cap.offer', provider='test', contract=contract, resource=resource,
                      configuration={'lease_class': 'persistent', 'stream': {
                          'enabled': True, 'lanes': ['control'], 'direction': 'bidirectional'}})
            lease = self.call('cap.acquire', task=resource, contract=contract, resource=resource)
            self.call('cap.fenced', provider='test', lease=lease['id'],
                      generation=lease['generation'])
            return self.call('stream.open', lease=lease['id'], **lifetime)

        persistent = open_control('relay.default')
        fixed = open_control('relay.fixed', ttl_seconds=3600)
        original_expiry = persistent['expires_at']
        self.assertEqual(original_expiry, fixed['expires_at'])
        self.now = original_expiry - 60
        context = Context(self.app, scope='site')
        self.app.stores['site'].execute(lambda tx: ScopedAuthority.renew_control_streams(tx, context))

        self.now = original_expiry + 1
        attached = self.call('stream.attach', session=persistent['id'], role='provider')
        for field in ('id', 'epoch', 'generation'):
            self.assertEqual(persistent[field], attached[field])
        self.assertGreater(attached['expires_at'], original_expiry)
        self.assertTrue(attached['attachment_credential'])
        with self.assertRaises(Fault):
            self.call('stream.attach', session=fixed['id'], role='provider')
