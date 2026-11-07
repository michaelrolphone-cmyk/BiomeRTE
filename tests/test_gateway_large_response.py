"""Exercise actual HTTP fetch/dispatch before result framing (Python 3.4.2+).

BIOME_GATEWAY_AGENT selects an exact deployment copy for qualification.
BIOME_GATEWAY_SUPPORT optionally supplies that deployment's support modules.
This test contacts only its own loopback HTTP server and uses dummy credentials.
"""
import hashlib
import json
import os
import runpy
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT = os.environ.get('BIOME_GATEWAY_AGENT', os.path.join(ROOT, 'deploy', 'gateway-agent.py'))
SUPPORT = os.environ.get('BIOME_GATEWAY_SUPPORT', os.path.join(ROOT, 'deploy'))
sys.path.insert(0, SUPPORT)
sys.path.insert(0, os.path.dirname(AGENT))


class LargeControllerResponse(unittest.TestCase):
    def test_full_fetch_dispatch_and_framing(self):
        agent = runpy.run_path(AGENT)
        result = {'content': [{'type': 'text', 'text': 'x' * 600000 + '\u2603\U0001f600' * 3000}],
                  'structuredContent': {'contracts': 124, 'offers': 125}}
        response = json.dumps({'jsonrpc': '2.0', 'id': 'large-read', 'result': result}).encode('utf-8')
        self.assertGreater(len(response), 548554)
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                requests.append(json.loads(body.decode('utf-8')))
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, *args):
                pass

        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()
        try:
            job = {'id': 'durable-large-read', 'lease': 'original-lease', 'request': {
                'name': 'biome_invoke', 'arguments': {'operation': 'cap.discover',
                'arguments': {}, 'request_id': 'large-read', 'realm': 'site'}}}
            actual = agent['dispatch'](job, 'http://127.0.0.1:' + str(server.server_port),
                                       {'admin_token': 'dummy-test-credential'})
            self.assertEqual(actual, result)
            self.assertEqual(requests[0]['params']['arguments']['operation'], 'cap.discover')
            # Import the framing helper only after actual HTTP dispatch succeeds:
            # a retained pre-framing read ceiling must fail this test first.
            import result_delivery
            frames = []

            def send(path, body):
                self.assertLess(len(json.dumps(body).encode('utf-8')), 500000)
                frames.append((path, body))
                return {'accepted': True}

            self.assertEqual(result_delivery.complete({'id': job['id'], 'lease': job['lease'],
                                                       'result': actual}, send), {'accepted': True})
            encoded = ''.join(body['data'] for path, body in frames if path == 'result-chunk')
            self.assertEqual(json.loads(encoded), result)
            self.assertEqual(frames[-1][0], 'result-commit')
            self.assertEqual(frames[-1][1]['digest'], hashlib.sha256(encoded.encode('ascii')).hexdigest())
            self.assertTrue(all(body['id'] == job['id'] and body['lease'] == job['lease']
                                for path, body in frames))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
