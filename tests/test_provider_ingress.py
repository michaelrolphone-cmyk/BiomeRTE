"""Native public ingress preserves scoped authentication and exact JSON."""
import io
import json
import os
import sys
import unittest
from urllib.error import HTTPError
from urllib.parse import urlsplit

from biomerte.identity import sign
from biomerte.store import Fault, encode, uid
from tests import test_runtime

sys.path.insert(0, os.path.join(test_runtime.ROOT, 'deploy'))
from provider_ingress import dispatch_signed_provider, dispatch_provider_pair


class Response(io.BytesIO):
    def getcode(self):
        return 200


class NativeController:
    def __init__(self, app):
        self.app, self.requests = app, []

    def open(self, request, timeout=None):
        self.requests.append(request)
        if request.get_header('Authorization') is not None:
            raise AssertionError('Provider ingress must never use owner authorization')
        body = json.loads(request.data.decode('utf-8'))
        try:
            path = urlsplit(request.full_url).path
            if path == '/v1/call':
                result = self.app.call(body['operation'], body['arguments'], body['id'],
                                       body['realm'], body['auth'])
            elif path == '/v1/pair/begin':
                result = self.app.begin_pair(body['public_key'], body['label'])
            elif path == '/v1/pair/finish':
                result = self.app.finish_pair(body)
            else:
                raise AssertionError('Unexpected native path')
            return Response(encode({'result': result}).encode('utf-8'))
        except Fault as error:
            raise HTTPError(request.full_url, error.status, 'Native rejection', {},
                            io.BytesIO(encode({'error': str(error)}).encode('utf-8')))


class ProviderIngress(unittest.TestCase):
    setUp = test_runtime.Runtime.setUp
    call = test_runtime.Runtime.call
    peer = test_runtime.Runtime.peer
    trust = test_runtime.Runtime.trust

    def prepare(self):
        self.device = self.peer()
        self.call('cap.define', {'name': 'relay.bank', 'inputs': {}, 'outputs': {}})
        self.trust(self.a, self.device, [
            'cap.offer', 'provider.poll', 'cap.fenced', 'provider.claim',
            'provider.result', 'cap.status', 'offer:relay.bank:1', 'resource:board'
        ])
        self.opener = NativeController(self.a)

    def packet(self, operation, arguments, rid=None):
        body = dict(site=self.a.site, realm='site', operation=operation,
                    arguments=arguments, id=rid or uid())
        body['auth'] = dict(actor=self.device.node, signature=sign(self.device.key, body))
        return encode(body)

    def forward(self, raw):
        result = dispatch_signed_provider({'envelope': raw}, 'http://127.0.0.1:8740',
                                          self.a.site, self.opener)
        return json.loads(result['content'][0]['text'])

    def test_signature_scope_revocation_and_json_survive_forwarding(self):
        self.prepare()
        args = dict(provider=self.device.node, contract='relay.bank:1',
                    resource='board', configuration={'fraction': 1.0, 'large': 9007199254740993})
        raw = self.packet('cap.offer', args, 'same-id')
        first = self.forward(raw)
        self.assertEqual(first['status'], 200)
        self.assertEqual(first['body']['result']['configuration'], args['configuration'])
        self.assertEqual(self.opener.requests[-1].data, raw.encode('utf-8'))
        self.assertEqual(self.forward(raw), first)

        forged = json.loads(raw)
        forged['arguments']['configuration']['large'] += 1
        self.assertNotEqual(self.forward(encode(forged))['status'], 200)
        other = dict(args, resource='ungranted')
        self.assertEqual(self.forward(self.packet('cap.offer', other))['status'], 403)
        self.call('peer.revoke', {'peer': self.device.node})
        # Same signed request and same previously successful mutation receipt:
        # revocation must be checked again rather than serving gateway cache.
        self.assertEqual(self.forward(raw)['status'], 403)

    def test_owner_operations_and_foreign_provider_are_never_forwarded(self):
        self.prepare()
        for op, args in [('peer.trust', {}), ('cap.define', {}),
                         ('provider.poll', {'provider': 'f' * 64})]:
            answer = self.forward(self.packet(op, args))
            self.assertEqual(answer['status'], 400)
        self.assertEqual(self.opener.requests, [])

    def test_candidate_requires_approval_and_private_key_proof(self):
        self.device = self.peer()
        self.opener = NativeController(self.a)
        def pair(phase, value):
            answer = dispatch_provider_pair({'phase': phase, 'envelope': encode(value)},
                                             'http://127.0.0.1:8740', self.a.site, self.opener)
            return json.loads(answer['content'][0]['text'])
        begin = {'public_key': self.device.config['public_key'], 'label': 'Biome Relay test'}
        challenge = pair('begin', begin)['body']['result']
        self.assertEqual(pair('begin', begin)['body']['result'], challenge)
        proof = dict(challenge, signature=sign(self.device.key, challenge))
        self.assertEqual(pair('finish', proof)['status'], 403)
        scopes = ['provider.poll']
        self.call('pair.approve', {'fingerprint': challenge['key'], 'scopes': scopes})
        finished = pair('finish', proof)
        self.assertEqual(finished['status'], 200)
        self.assertEqual(finished['body']['result']['credential'], self.device.node)
        self.assertEqual(finished['body']['result']['scopes'], scopes)
        self.assertEqual(pair('finish', proof), finished)
        self.assertEqual(pair('approve', {'fingerprint': self.device.node})['status'], 400)

    def test_small_poll_pages_keep_every_action(self):
        self.prepare()
        def native(op, args):
            response = self.forward(self.packet(op, args))
            self.assertEqual(response['status'], 200)
            return response['body']['result']
        native('cap.offer', {'provider': self.device.node, 'contract': 'relay.bank:1',
                             'resource': 'board'})
        lease = self.call('cap.acquire', {'task': 'caller', 'contract': 'relay.bank:1'})
        native('cap.fenced', {'provider': self.device.node, 'lease': lease['id'],
                             'generation': lease['generation']})
        for operation in ('one', 'two', 'three'):
            self.call('cap.invoke', {'lease': lease['id'], 'operation': operation, 'inputs': {}})
        self.assertEqual(len(native('provider.poll', {'provider': self.device.node})['actions']), 3)
        completed = set()
        for unused in range(3):
            actions = native('provider.poll', {'provider': self.device.node, 'limit': 1})['actions']
            self.assertEqual(len(actions), 1)
            action = actions[0]
            args = dict(provider=self.device.node, operation=action['id'], generation=action['generation'])
            native('provider.claim', args)
            native('provider.result', dict(args, state='succeeded', result={}))
            completed.add(action['id'])
        self.assertEqual(completed, set(('one', 'two', 'three')))
        self.assertEqual(native('provider.poll', {'provider': self.device.node, 'limit': 1})['actions'], [])
        for invalid in (0, -1, True):
            response = self.forward(self.packet('provider.poll', {'provider': self.device.node, 'limit': invalid}))
            self.assertEqual(response['status'], 400)


if __name__ == '__main__':
    unittest.main()
