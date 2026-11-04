import copy
import unittest
from cryptography.exceptions import InvalidTag
from crypto_wire import *
class WireTests(unittest.TestCase):
    def setUp(self):
        self.key = bytes(range(32))
        self.identity = '00000000-0000-4000-8000-000000000001'
        self.value = {'messages': [{'role': 'user', 'content': 'private café 🧬'}]}
    def test_round_trip_and_no_plaintext(self):
        e = seal(self.key, 'request', self.identity, self.value)
        self.assertEqual(open_envelope(self.key, 'request', e), self.value)
        self.assertNotIn('private', json.dumps(e))
    def test_tampering_identity_direction_key(self):
        e = seal(self.key, 'request', self.identity, self.value)
        bad = dict(e, id='00000000-0000-4000-8000-000000000002')
        for key, kind, envelope in [(self.key, 'request', bad), (self.key, 'response', e), (bytes(32), 'request', e)]:
            with self.assertRaises(InvalidTag):
                open_envelope(key, kind, envelope)
        e['ct'] = b64(unb64(e['ct'])[:-1] + bytes([unb64(e['ct'])[-1] ^ 1]))
        with self.assertRaises(InvalidTag):
            open_envelope(self.key, 'request', e)
    def test_nonce_uniqueness(self):
        self.assertNotEqual(seal(self.key,'request',self.identity,self.value)['nonce'],seal(self.key,'request',self.identity,self.value)['nonce'])
if __name__ == '__main__':
    unittest.main()
