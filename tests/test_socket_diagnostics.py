import io
import os
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'deploy'))
from persistent_link import ControllerLink

class SocketDiagnosticsTests(unittest.TestCase):
    def test_failure_records_type_and_status_without_exception_secrets(self):
        class HandshakeFailure(Exception):
            status_code = 403
        link = ControllerLink.__new__(ControllerLink)
        output = io.StringIO()
        with patch('persistent_link.sys.stderr', output):
            link._diagnostic('disconnected', HandshakeFailure('Bearer private-secret https://private'))
        text = output.getvalue()
        self.assertIn('HandshakeFailure', text)
        self.assertIn('403', text)
        self.assertNotIn('private-secret', text)
        self.assertNotIn('https://', text)
        self.assertEqual(link.last_transport_event['event'], 'disconnected')

if __name__ == '__main__':
    unittest.main()
