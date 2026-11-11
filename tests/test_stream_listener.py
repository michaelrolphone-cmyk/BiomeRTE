import unittest
from biomerte.stream_router import listener_tls

class StreamListenerTests(unittest.TestCase):
    def test_private_transport_only_binds_loopback(self):
        for host in ('127.0.0.1', '::1'):
            self.assertIsNone(listener_tls({'transport': 'private_http', 'listen': host}))
        for host in ('0.0.0.0', '::', '192.168.1.1', 'localhost'):
            with self.assertRaises(ValueError):
                listener_tls({'transport': 'private_http', 'listen': host})

    def test_public_transport_requires_certificate(self):
        with self.assertRaises(KeyError):
            listener_tls({})
