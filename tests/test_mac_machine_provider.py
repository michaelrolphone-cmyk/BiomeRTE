import importlib.util
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from biomerte.app import Application, initialize
from biomerte.store import Fault


class MacMachineProvider(unittest.TestCase):
    def setUp(self):
        scratch = self.enter_temp_directory()
        transport = Path(__file__).resolve().parents[1] / 'providers/mac-terminal/mac_provider.py'
        spec = importlib.util.spec_from_file_location('mac_machine_provider', transport)
        self.client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.client)
        self.client.ROOT = scratch / 'provider'
        self.client.ROOT.mkdir()
        self.client.CREDS = self.client.ROOT / 'oauth.json'
        self.provider = 'e' * 64
        self.resource = 'storage.mac.test'
        self.contract = 'storage.test:1'
        home = str(scratch / 'controller')
        site = initialize(home)['site']
        self.app = Application(home)
        self.client.save_json(self.client.ROOT / 'terminal-manifest.json', {'site': site})
        self.admin('cap.define', {'name': 'storage.test', 'inputs': {}, 'outputs': {}})
        self.admin('cap.offer', {'provider': self.provider, 'resource': self.resource, 'contract': self.contract})

    def enter_temp_directory(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name)

    def admin(self, operation, arguments):
        return self.app.call(operation, arguments, 'setup-' + operation)

    def forward(self, url, envelope):
        self.assertEqual(url, self.client.ORIGIN + '/v1/call')
        try:
            result = self.app.call(envelope['operation'], envelope['arguments'], envelope['id'],
                                   envelope['realm'], envelope['auth'])
        except Fault as error:
            raise self.client.HTTPFailure(error.status, {'error': str(error)}) from error
        return {'result': result}

    def bind(self, candidate):
        return self.admin('peer.bind_provider_key', dict(public_key=candidate['public_key'],
                          provider=self.provider, resources=[self.resource], contracts=[self.contract]))

    def test_headless_enrollment_and_reconnect_with_spent_or_missing_oauth(self):
        prepare = self.client.prepare_machine_key
        candidate = prepare(self.provider)
        self.assertEqual(prepare(self.provider), candidate)
        (self.client.ROOT / 'refresh-pending.json').write_text('{}')
        self.assertEqual(self.client.machine_probe(self.provider)['state'], 'authorization_lost')
        self.bind(candidate)
        with mock.patch.object(self.client, 'request', side_effect=self.forward), \
                mock.patch.object(self.client, 'credentials', side_effect=AssertionError('OAuth called')):
            receipt = self.client.activate_machine_key(self.provider, candidate['actor'])
            self.assertEqual(receipt, self.client.machine_probe(self.provider))
            value = self.client.invoke_provider('provider.poll', {'provider': self.provider}, provider=self.provider)
            self.assertEqual(value['actions'], [])
            self.assertEqual(self.client.machine_probe(self.provider), receipt)

    def test_key_cannot_change_provider_or_offer_authority_and_revocation_has_no_oauth_fallback(self):
        candidate = self.client.prepare_machine_key(self.provider)
        self.bind(candidate)
        with mock.patch.object(self.client, 'request', side_effect=self.forward), \
                mock.patch.object(self.client, 'credentials', side_effect=AssertionError('OAuth called')):
            self.client.activate_machine_key(self.provider, candidate['actor'])
            for operation, arguments in (
                ('provider.poll', {'provider': 'a' * 64}),
                ('cap.offer', {'provider': self.provider, 'contract': self.contract, 'resource': 'other'}),
                ('cap.define', {'name': 'storage.other', 'inputs': {}, 'outputs': {}}),
                ('peer.revoke_provider_key', {'actor': candidate['actor']}),
            ):
                with self.subTest(operation=operation), self.assertRaises(self.client.ControllerOperationError) as failure:
                    self.client.invoke_provider(operation, arguments, provider=self.provider)
                self.assertEqual(failure.exception.status, 403)
            self.admin('peer.revoke_provider_key', {'actor': candidate['actor']})
            with self.assertRaises(self.client.ControllerOperationError) as failure:
                self.client.machine_probe(self.provider)
            self.assertEqual(failure.exception.status, 403)
            health = self.client.ROOT / ('machine-auth-' + self.provider + '.json')
            self.assertIn('authorization_lost', health.read_text())

    def test_lost_reply_retries_the_exact_signed_request(self):
        candidate = self.client.prepare_machine_key(self.provider)
        self.bind(candidate)
        with mock.patch.object(self.client, 'request', side_effect=self.forward):
            self.client.activate_machine_key(self.provider, candidate['actor'])
        offer = {'provider': self.provider, 'resource': self.resource, 'contract': self.contract}
        delivered = []

        def lose_first_reply(url, envelope):
            delivered.append(envelope.copy())
            result = self.forward(url, envelope)
            if len(delivered) == 1:
                raise ConnectionError('reply lost after commit')
            return result

        with mock.patch.object(self.client, 'request', side_effect=lose_first_reply), \
                mock.patch.object(self.client.time, 'sleep'):
            result = self.client.invoke_provider('cap.offer', offer, 'preserved-request', provider=self.provider)
        self.assertEqual(delivered[0], delivered[1])
        self.assertEqual(result['provider'], self.provider)

    def test_scoped_provider_restores_existing_publication_and_dependency_definition(self):
        candidate = self.client.prepare_machine_key(self.provider)
        dependent = {'name': 'existing.dependency', 'inputs': {}, 'outputs': {}}
        self.app.call('cap.define', dependent, 'define-dependency')
        self.admin('peer.bind_provider_key', dict(public_key=candidate['public_key'], provider=self.provider,
                   resources=[self.resource], contracts=[self.contract], definitions=['existing.dependency:1']))
        scope = dict(provider=self.provider, resource=self.resource, service='')
        with mock.patch.object(self.client, 'request', side_effect=self.forward):
            self.client.activate_machine_key(self.provider, candidate['actor'])

            def call(operation, arguments):
                return self.client.invoke_provider(operation, arguments, provider=self.provider)

            reply = call('cap.registrations', dict(scope, contracts=[self.contract]))
            receipt = reply['registrations'][0]['registration']
            call('cap.withdraw', dict(scope, registrations={self.contract: receipt}))
            reply = call('cap.registrations', dict(scope, contracts=[self.contract]))
            receipt = reply['registrations'][0]['registration']
            entries = [dict(definition={'name': 'storage.test', 'inputs': {}, 'outputs': {}},
                            offer={'contract': self.contract, 'state': 'ready'}, expected_registration=receipt)]
            restored = call('cap.publish', dict(scope, offers=entries))
            self.assertEqual(restored['offers'][0]['status'], 'published')
            self.assertEqual(call('cap.define', dependent)['id'], 'existing.dependency:1')
            with self.assertRaises(self.client.ControllerOperationError):
                call('cap.offer', dict(scope, contract='existing.dependency:1'))

    def test_controller_receipt_retirement_is_visible_to_existing_reconcilers(self):
        candidate = self.client.prepare_machine_key(self.provider)
        self.bind(candidate)
        with mock.patch.object(self.client, 'request', side_effect=self.forward):
            self.client.activate_machine_key(self.provider, candidate['actor'])
        message = 'Receipt result retired; request will not execute again'
        error = self.client.HTTPFailure(410, {'error': message})
        with mock.patch.object(self.client, 'request', side_effect=error):
            with self.assertRaises(self.client.ControllerOperationError) as failure:
                self.client.invoke_provider('cap.offer', {}, provider=self.provider)
        self.assertEqual(failure.exception.status, 410)
        self.assertEqual(str(failure.exception), message)

    def test_legacy_refresh_journal_clears_only_after_replacement_is_durable(self):
        previous = 'mock-original-refresh'
        marker = self.client.ROOT / 'refresh-pending.json'
        self.client.save_json(self.client.CREDS, {'refresh_token': previous, 'expires_at': float('inf')})
        self.client.save_json(marker, {'refresh_sha256': hashlib.sha256(previous.encode()).hexdigest()})
        with mock.patch.object(self.client, 'request', side_effect=AssertionError('Refresh replayed')):
            with self.assertRaises(self.client.OAuthAuthorizationLost):
                self.client.credentials()
            self.assertTrue(marker.exists())
            self.client.save_json(self.client.CREDS, {'refresh_token': 'mock-rotated-refresh', 'expires_at': float('inf')})
            self.assertEqual(self.client.credentials()['refresh_token'], 'mock-rotated-refresh')
            self.assertFalse(marker.exists())


if __name__ == '__main__':
    unittest.main()
