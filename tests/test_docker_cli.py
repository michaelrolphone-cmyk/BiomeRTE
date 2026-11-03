"""Exercise native argv, stdin, result, and process limits without a host daemon."""
import base64
import hashlib
import json
import os
import runpy
import subprocess
import sys
import time
import tempfile
import unittest

root = os.path.join(os.path.dirname(__file__), '..', 'providers', 'pi-docker')
adapter = runpy.run_path(os.path.join(root, 'docker_cli.py'))


class Artifacts:
    def __init__(self):
        self.blocks = {}

    def call(self, op, args, request_id=None):
        if op == 'artifact.get':
            return {'data': self.blocks[args['sha256']]}
        data = base64.b64decode(args['data'])
        sha = hashlib.sha256(data).hexdigest()
        self.blocks[sha] = args['data']
        return {'sha256': sha, 'size': len(data)}


class DockerCLI(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = dict(executable=sys.executable, spool=tmp.name, workspace=tmp.name,
                           client_config=tmp.name, socket='/var/run/docker.sock')
        self.remote = Artifacts()

    def call(self, **args):
        return adapter['execute'](self.config, args, self.remote)

    def test_passthrough_exit_and_separate_streams(self):
        script = 'import sys,os,json;print(json.dumps([sys.argv[1:],os.getcwd()]));sys.stderr.write("failure");sys.exit(17)'
        flags = ['--unknown-future-flag', 'a b', '$HOME', ';', '']
        out = self.call(argv=['-c', script] + flags)
        self.assertTrue(out['executed'])
        self.assertEqual(json.loads(out['stdout']), [flags, os.path.realpath(self.config['workspace'])])
        self.assertEqual((out['exit_code'], out['stderr']), (17, 'failure'))
        self.assertFalse(out['timed_out'])

    def test_raw_binary_roundtrip_by_chunk_reference(self):
        data = bytes(bytearray(range(256))) * 1025
        argv = ['-c', 'import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())']
        out = self.call(argv=argv, stdin_base64=base64.b64encode(data).decode())
        stream = out['streams']['stdout']
        self.assertEqual(stream['sha256'], hashlib.sha256(data).hexdigest())
        self.assertTrue(stream['preview_truncated'])
        self.assertFalse(stream['truncated'])
        self.assertEqual(stream['total_bytes'], len(data))
        replay = self.call(argv=argv, stdin_chunks=stream['chunks'])
        self.assertEqual(replay['streams']['stdout'], stream)

    def test_gateway_output_chunk_size(self):
        self.config['chunk_bytes'] = 48 * 1024
        out = self.call(argv=['-c', 'import sys; sys.stdout.buffer.write(b"x" * 100000)'])
        stream = out['streams']['stdout']
        self.assertEqual([c['size'] for c in stream['chunks']], [49152, 49152, 1696])
        self.assertEqual(stream['total_bytes'], 100000)

    def test_invalid_input_cannot_launch(self):
        for args in [dict(argv=['bad\0arg']), dict(argv=[3]), dict(argv=[], cwd='/definitely/missing/pi-path'),
                     dict(argv=[], stdin='a', stdin_base64='b'), dict(argv=[], stdin_base64='!'),
                     dict(argv=[], stdin_chunks=['not-a-digest']), dict(argv=[], timeout_seconds=0)]:
            self.assertFalse(self.call(**args)['executed'])

    def test_excess_output_is_drained_and_counted(self):
        proc = subprocess.Popen([sys.executable, '-c', 'print("abcdef")'], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
        paths, totals, expired = adapter['capture'](proc, self.config['spool'], time.monotonic() + 5, 3)
        with open(paths['stdout'], 'rb') as captured:
            self.assertEqual(captured.read(), b'abc')
        self.assertEqual((totals['stdout'], expired), (7, False))

    def test_timeout_returns_captured_output(self):
        out = self.call(argv=['-c', 'import sys,time;print("started");sys.stdout.flush();time.sleep(9)'],
                        timeout_seconds=1)
        self.assertTrue(out['timed_out'])
        self.assertNotEqual(out['exit_code'], 0)
        self.assertEqual(out['stdout'], 'started\n')
