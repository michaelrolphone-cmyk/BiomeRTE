import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy'))
from persistent_link import ControllerLink


class SocketDiagnosticsTests(unittest.TestCase):
    def test_failure_records_status_without_exception_secrets(self):
        class HandshakeFailure(Exception):
            status_code = 403

        link = ControllerLink.__new__(ControllerLink)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'transport.json'
            link.status_path = str(path)
            link.status_lock = threading.Lock()
            link.transport_status = {'connected': True, 'errors': 0}
            link._diagnostic('disconnected', HandshakeFailure('Bearer private-secret https://private'))

            text = path.read_text()
            value = json.loads(text)
            self.assertEqual(value['event'], 'disconnected')
            self.assertEqual(value['last_http_status'], 403)
            self.assertEqual(value['last_error'], 'TransportError')
            self.assertNotIn('private-secret', text)
            self.assertNotIn('https://', text)
