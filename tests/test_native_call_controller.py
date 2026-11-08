"""Real loopback controller authorization/receipt tests. Set BIOMERTE_TEST_ROOT."""
import json
import os
import sys
import tempfile
import threading
import unittest
from test_native_call import relay

root = os.environ.get('BIOMERTE_TEST_ROOT', os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
if os.path.isdir(os.path.join(root, 'biomerte')):
    sys.path.insert(0, root)
    from biomerte.app import initialize, Application
    from biomerte.web import server
    from biomerte.identity import sign
    AVAILABLE = True
else:
    AVAILABLE = False


@unittest.skipUnless(AVAILABLE, 'Set BIOMERTE_TEST_ROOT to the existing controller source')
class ControllerAuthorization(unittest.TestCase):
    def test_scope_forgery_receipt_replay_and_revocation(self):
        with tempfile.TemporaryDirectory() as home:
            controller_home = os.path.join(home, 'controller')
            device_home = os.path.join(home, 'device')
            initialize(controller_home)
            device = initialize(device_home)
            app = Application(controller_home)
            app.call('peer.trust', {'label': 'test device', 'public_key': device['public_key'],
                                  'scopes': ['work.create', 'system.info']}, 'trust')
            srv = server(app, '127.0.0.1', 0)
            thread = threading.Thread(target=srv.serve_forever)
            thread.daemon = True
            thread.start()
            local = 'http://127.0.0.1:' + str(srv.server_port)

            def request(operation, arguments, ident):
                payload = {'site': app.site, 'realm': 'site', 'operation': operation,
                           'arguments': arguments, 'id': ident}
                payload['auth'] = {'actor': device['node'], 'signature': sign(os.path.join(device_home, 'node.key'), payload)}
                return payload

            def forward(payload):
                result = relay.dispatch_native_call({'site': app.site, 'body': json.dumps(payload)}, local, app.config)
                return json.loads(result['content'][0]['text'])

            try:
                payload = request('work.create', {'title': 'one effect', 'kind': 'task'}, 'durable-write')
                first = forward(payload)
                self.assertEqual(first['status'], 200)
                self.assertEqual(forward(payload), first)
                denied = forward(request('work.list', {}, 'scope-denied'))
                self.assertEqual(denied['status'], 403)
                forged = request('system.info', {}, 'forged')
                forged['auth']['signature'] = 'AAAA'
                self.assertGreaterEqual(forward(forged)['status'], 400)
                app.call('peer.revoke', {'peer': device['node']}, 'revoke')
                self.assertEqual(forward(payload)['status'], 403)
            finally:
                srv.shutdown()
                srv.server_close()
                thread.join()
