"""Transport status is bounded, safe and independent of command custody."""
import contextlib
import io
import json
import os
import pathlib
import queue
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'deploy'))
import persistent_link as link

class ControllerTransportStatusTests(unittest.TestCase):
    def make(self, path):
        with patch.object(link.threading.Thread, 'start'):
            return link.ControllerLink('https://example.invalid', 'secret-token', 'secret-site', path)

    def test_snapshot_tracks_socket_delivery_without_sensitive_payloads(self):
        with tempfile.TemporaryDirectory() as home:
            path = os.path.join(home, 'status.json')
            c = self.make(path)
            c._diagnostic('connected')
            ws = object()
            c._receive(ws, {'type':'job', 'id':'secret-job', 'lease':'secret-lease',
                            'request':{'token':'secret-body'}})
            event = threading.Event()
            c.pending['secret-job'] = {'socket':ws, 'lease':'secret-lease', 'ready':event}
            c._receive(ws, {'type':'accepted', 'id':'secret-job', 'lease':'secret-lease', 'accepted':True})
            self.assertTrue(event.is_set())
            self.assertEqual(c.jobs.get_nowait()['_delivery_socket'], ws)
            c._diagnostic('disconnected')
            raw = pathlib.Path(path).read_text()
            value = json.loads(raw)
            self.assertFalse(value['connected'])
            self.assertEqual(value['connections'], 1)
            self.assertEqual(value['socket_jobs'], 1)
            self.assertEqual(value['socket_acks'], 1)
            self.assertNotIn('secret', raw)
            self.assertNotIn('example.invalid', raw)
            self.assertLess(len(raw), 1024)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(os.listdir(home), ['status.json'])

    def test_error_headers_and_messages_never_enter_status_or_stdout(self):
        with tempfile.TemporaryDirectory() as home:
            path = os.path.join(home, 'status.json')
            c = self.make(path)
            error = type('WebSocketBadStatusException', (Exception,), {} )('secret headers and token')
            error.status_code = 403
            error.resp_headers = {'Authorization':'secret'}
            with contextlib.redirect_stdout(io.StringIO()) as output:
                c._diagnostic('disconnected', error)
            value = json.loads(pathlib.Path(path).read_text())
            self.assertEqual(value['last_http_status'], 403)
            self.assertEqual(value['last_error'], 'WebSocketBadStatusException')
            self.assertNotIn('secret', json.dumps(value))
            self.assertEqual(output.getvalue(), '')

    def test_hot_path_is_throttled_and_storage_failures_do_not_break_delivery(self):
        with tempfile.TemporaryDirectory() as home:
            c = self.make(os.path.join(home, 'status.json'))
            c._diagnostic('connected')
            with patch.object(link.os, 'replace', side_effect=AssertionError('Hot path wrote status')):
                for unused in range(100):
                    c._diagnostic('job')
            self.assertEqual(c.transport_status['socket_jobs'], 100)
            c.status_path = os.path.join(home, 'absent', 'status.json')
            c._diagnostic('disconnected', OSError('secret-error'))
            self.assertFalse(c.transport_status['connected'])

    def test_legacy_clients_have_no_new_storage_requirement(self):
        c = link.ControllerLink.__new__(link.ControllerLink)
        c._diagnostic('connected')
        c._diagnostic('unknown', ValueError('secret'))

if __name__ == '__main__':
    unittest.main()
