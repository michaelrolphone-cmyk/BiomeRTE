"""Controller regressions for the Python 3.4.2 deployment target."""
import ast
import base64
import hashlib
import json
import os
import runpy
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler
from unittest.mock import patch

from biomerte import assistant_support, assistant_workspaces, gateway_journal
from biomerte.store import Fault


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Python34Compatibility(unittest.TestCase):
    def test_controller_import_without_path_read_text(self):
        source = """import pathlib
def unavailable(*args, **kwargs):
    raise AssertionError('Path.read_text requires Python 3.5')
pathlib.Path.read_text = unavailable
import biomerte.app
assert biomerte.app.assistant_support.CAT
assert biomerte.app.assistant_support.assistant_workspaces.SCHEMAS
"""
        subprocess.check_call([sys.executable, '-B', '-S', '-c', source], cwd=ROOT)

    def test_controller_has_no_extended_container_unpacking(self):
        # The workspace runner is source executed in a modern workspace, not
        # imported by the controller. Its interpreter is a separate target.
        directory = os.path.join(ROOT, 'biomerte')
        for name in os.listdir(directory):
            if not name.endswith('.py') or name == 'assistant_workspace_runner.py':
                continue
            with open(os.path.join(directory, name), encoding='utf-8') as stream:
                tree = ast.parse(stream.read(), filename=name)
            for node in ast.walk(tree):
                if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
                    self.assertFalse(any(isinstance(x, ast.Starred) for x in node.elts), name)
                if isinstance(node, ast.Dict):
                    self.assertNotIn(None, node.keys, name)

    def test_terminal_arguments_preserve_chunked_json(self):
        arguments = {'message': '\u2603' * 3000, 'quote': "'\"$\\"}
        cfg = {'controller_home': '/tmp/biome test',
               'controller_source_dir': ROOT, 'python_executable': sys.executable}
        with patch.object(assistant_support, 'select_resource', return_value='terminal.test'):
            action = assistant_support.cli_action(None, cfg, 'work.list', arguments)
        argv = action['inputs']['argv']
        self.assertEqual(argv[:2], ['/bin/bash', '-c'])
        self.assertEqual(argv[3], 'biome-assistant')
        self.assertGreater(len(argv[4:]), 1)
        self.assertTrue(all(len(chunk) <= 4000 for chunk in argv[4:]))
        self.assertEqual(json.loads(''.join(argv[4:])), arguments)

    def test_artifact_json_is_decoded_to_text(self):
        expected = {'stdout': '\u2603', 'exit_code': 0}
        encoded = json.dumps(expected, ensure_ascii=False).encode('utf-8')
        sha = hashlib.sha256(encoded).hexdigest()
        manifest = {'size': len(encoded), 'sha256': sha,
                    'chunks': [{'size': len(encoded), 'sha256': sha}]}
        loads = json.loads

        def text_only(value):
            self.assertIsInstance(value, str)
            return loads(value)

        with patch.object(assistant_workspaces, 'invoke', return_value={
                'data': base64.b64encode(encoded).decode('ascii')}):
            with patch.object(assistant_workspaces.json, 'loads', side_effect=text_only):
                result = assistant_workspaces.decode(None, None, {'result': {'artifact': manifest}})
        self.assertEqual(result, expected)


class GatewayTLSCompatibility(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.key = os.path.join(cls.directory.name, 'key.pem')
        certificate = os.path.join(cls.directory.name, 'cert.pem')
        with open(os.devnull, 'wb') as quiet:
            subprocess.check_call(['openssl', 'req', '-x509', '-newkey', 'rsa:2048',
                                   '-nodes', '-keyout', cls.key, '-out', certificate,
                                   '-days', '1', '-subj', '/CN=localhost'],
                                  stdout=quiet, stderr=quiet)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header('Location', '/followed')
                self.end_headers()

            def log_message(self, *args):
                pass

        cls.http = HTTPServer(('127.0.0.1', 0), Handler)
        cls.https = HTTPServer(('127.0.0.1', 0), Handler)
        context = ssl.SSLContext(getattr(ssl, 'PROTOCOL_TLS_SERVER', ssl.PROTOCOL_SSLv23))
        context.load_cert_chain(certificate, cls.key)
        cls.https.socket = context.wrap_socket(cls.https.socket, server_side=True)
        cls.threads = []
        for server in (cls.http, cls.https):
            thread = threading.Thread(target=server.serve_forever)
            thread.daemon = True
            thread.start()
            cls.threads.append(thread)
        deploy = os.path.join(ROOT, 'deploy')
        with patch.object(sys, 'path', [deploy] + sys.path):
            cls.agent = runpy.run_path(os.path.join(deploy, 'gateway-agent.py'))

    @classmethod
    def tearDownClass(cls):
        for server in (cls.http, cls.https):
            server.shutdown()
            server.server_close()
        for thread in cls.threads:
            thread.join(5)
        cls.directory.cleanup()

    def test_gateway_agent_verifies_tls_and_rejects_redirects(self):
        handlers = [h for h in self.agent['opener'].handlers if isinstance(h, HTTPSHandler)]
        self.assertEqual(len(handlers), 1)
        context = handlers[0]._context
        self.assertIsNotNone(context)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        with self.assertRaises(URLError) as caught:
            self.agent['fetch']('https://127.0.0.1:%d/' % self.https.server_port)
        self.assertIsInstance(caught.exception.reason, ssl.SSLError)
        with self.assertRaises(HTTPError) as caught:
            self.agent['fetch']('http://127.0.0.1:%d/' % self.http.server_port)
        self.assertEqual(caught.exception.code, 302)

    def test_gateway_journal_uses_explicit_verified_tls(self):
        token = os.path.join(self.directory.name, 'test-token')
        with open(token, 'w') as stream:
            stream.write('test-only')
        journal = gateway_journal.GatewayJournal({
            'origin': 'https://127.0.0.1:%d' % self.https.server_port,
            'key_file': token}, 'test-site')
        create_context = ssl.create_default_context
        contexts = []

        def verified_context():
            context = create_context()
            contexts.append(context)
            return context

        with patch.object(gateway_journal.ssl, 'create_default_context', side_effect=verified_context):
            with self.assertRaises(Fault) as caught:
                journal.request('status', {})
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(contexts[0].check_hostname)
