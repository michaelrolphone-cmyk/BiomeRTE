import types
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

source = os.path.join(os.path.dirname(__file__), '..', 'deploy', 'native_call.py')
relay = types.ModuleType('native_call')
with open(source, encoding='utf-8') as handle:
    exec(compile(handle.read(), source, 'exec'), relay.__dict__)



class Reply(io.BytesIO):
    code = 200


class NativeRelayTest(unittest.TestCase):
    def setUp(self):
        self.wire = '{"site":"site","id":"abc","realm":"site","operation":"provider.poll","arguments":{"n":1.0},"auth":{"actor":"device","signature":"sig"}}'
        self.args = {'site': 'site', 'realm': 'site', 'body': self.wire}
        self.conf = {'site': 'site', 'admin_token': 'must-not-be-transmitted'}

    def test_original_bytes_no_admin_or_site_token(self):
        with patch.object(relay, 'build_opener') as factory:
            factory.return_value.open.return_value = Reply(b'{"result":{"actions":[]}}')
            result = relay.dispatch_native_call(self.args, 'http://127.0.0.1:8030', self.conf)
            request = factory.return_value.open.call_args[0][0]
            self.assertEqual(request.full_url, 'http://127.0.0.1:8030/v1/call')
            self.assertEqual(request.data, self.wire.encode())
            self.assertEqual(dict(request.header_items()), {'Content-type': 'application/json'})
            self.assertEqual(json.loads(result['content'][0]['text'])['body'], {'result': {'actions': []}})

    def test_controller_denial_is_preserved(self):
        with patch.object(relay, 'build_opener') as factory:
            factory.return_value.open.side_effect = HTTPError('local', 403, 'Denied', {}, io.BytesIO(b'{"error":"Revoked credential"}'))
            result = relay.dispatch_native_call(self.args, 'http://localhost:8030', self.conf)
            self.assertEqual(json.loads(result['content'][0]['text'])['status'], 403)

    def test_server_and_network_failures_remain_retryable(self):
        for error in [TimeoutError(), HTTPError('local', 503, 'Unavailable', {}, io.BytesIO(b'{}'))]:
            with patch.object(relay, 'build_opener') as factory:
                factory.return_value.open.side_effect = error
                with self.assertRaises(type(error)):
                    relay.dispatch_native_call(self.args, 'http://localhost:8030', self.conf)

    def test_wrong_site_local_realm_and_remote_route_are_rejected_before_network(self):
        for args, local in [(dict(self.args, site='other'), 'http://localhost:8030'),
                            (self.args, 'https://evil.test'),
                            (dict(self.args, body=self.wire.replace('"realm":"site"','"realm":"local"')), 'http://localhost:8030')]:
            with patch.object(relay, 'build_opener') as factory:
                with self.assertRaises(ValueError):
                    relay.dispatch_native_call(args, local, self.conf)
                self.assertFalse(factory.called)

    def test_actual_loopback_http_bytes_status_and_headers(self):
        records = []
        replies = [(200, {"result": {"text": "caf\u00e9"}}),
                   (403, {"error": "Revoked credential"})]

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                wire = self.rfile.read(int(self.headers['Content-Length']))
                records.append((self.path, wire, dict(self.headers.items())))
                status, body = replies[len(records) - 1]
                encoded = json.dumps(body, ensure_ascii=False).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *args):
                pass

        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()
        try:
            local = 'http://127.0.0.1:' + str(server.server_address[1])
            for status, body in replies:
                result = relay.dispatch_native_call(self.args, local, self.conf)
                self.assertEqual(json.loads(result['content'][0]['text']),
                                 {'status': status, 'body': body})
            for path, wire, headers in records:
                self.assertEqual(path, '/v1/call')
                self.assertEqual(wire, self.wire.encode('utf-8'))
                names = {name.lower() for name in headers}
                self.assertNotIn('authorization', names)
                self.assertNotIn('oai-sites-authorization', names)
                self.assertEqual(headers['Content-Type'], 'application/json')
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_redirects_never_forward_device_envelope_elsewhere(self):
        self.assertIsNone(relay.NoNativeRedirect().redirect_request(None, None, 307, '', {}, 'https://evil.test'))


if __name__ == '__main__':
    unittest.main()
