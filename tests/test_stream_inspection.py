import hashlib
import hmac
import json
import unittest
from unittest.mock import patch, MagicMock
from biomerte.stream_inspection import add_remote, inspect_remote, loopback_url


class StreamInspectionTests(unittest.TestCase):
    def test_only_explicit_loopback_transport_can_override_public_descriptor(self):
        self.assertIsNone(loopback_url(None))
        self.assertEqual(loopback_url('ws://127.0.0.1:8751/'), 'ws://127.0.0.1:8751')
        for value in ('ws://public.example', 'http://127.0.0.1', 'ws://localhost/path',
                      'ws://user@localhost', 'ws://localhost?token=x'):
            with self.assertRaises(ValueError): loopback_url(value)

    def test_remote_validation_cannot_replace_local_or_use_plain_http(self):
        node='a'*64
        settings={'secret':'s'*40,'inspect_url':'https://gateway/inspect'}
        with self.assertRaises(ValueError):add_remote({'remote_controllers':{node:settings}}, {node:'local'}, {})
        with self.assertRaises(ValueError):add_remote({'remote_controllers':{node:{**settings,'inspect_url':'http://gateway'}}}, {}, {})
        secrets, validators={}, {}
        add_remote({'remote_controllers':{node:settings}},secrets,validators)
        self.assertEqual(secrets[node], settings['secret'])
        self.assertIn(node, validators)

    def test_inspection_authenticates_exact_session_authority_and_timestamp(self):
        settings={'secret':'s'*40,'inspect_url':'https://gateway/inspect'}
        response=MagicMock()
        response.__enter__.return_value.read.return_value=b'{"state":"open"}'
        with patch('urllib.request.urlopen',return_value=response) as request:
            self.assertEqual(inspect_remote('a'*64,settings,'b'*32),{'state':'open'})
        req=request.call_args.args[0]
        body=json.loads(req.data)
        self.assertEqual(body['session'],'b'*32)
        self.assertEqual(body['authority'],'a'*64)
        expected=hmac.new(settings['secret'].encode(),req.data,hashlib.sha256).hexdigest()
        self.assertEqual(req.get_header('Authorization'),'Router '+expected)
