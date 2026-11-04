"""Compatibility checks for the optional external Pi terminal provider."""
import base64
import hashlib
import os
import tempfile
import unittest

import runpy
adapter = runpy.run_path(os.path.join(os.path.dirname(__file__), '..', 'providers',
                                      'pi-terminal', 'terminal.py'))


class TerminalTests(unittest.TestCase):
    def test_digest_pin_and_bounds(self):
        with tempfile.NamedTemporaryFile() as shell:
            profile = {'executable': os.path.realpath(shell.name),
                       'executable_sha256': hashlib.sha256(b'').hexdigest()}
            check = adapter['check_argv']
            self.assertEqual(check([shell.name, '-c', ''], profile), [profile['executable'], '-c', ''])
            for bad in ([], ['bash'], ['/bin/false'], [shell.name, 3], [shell.name, 'x' * 4097],
                        [shell.name] * 33, [shell.name, 'a\x00b']):
                with self.assertRaises(ValueError):
                    check(bad, profile)
            shell.write(b'changed')
            shell.flush()
            with self.assertRaises(ValueError):
                check([shell.name], profile)

    def test_chunked_binary_output(self):
        class Artifacts:
            def __init__(self):
                self.parts = []

            def call(self, name, args, rid):
                block = base64.b64decode(args['data'])
                self.parts.append(block)
                return {'sha256': hashlib.sha256(block).hexdigest(), 'size': len(block)}

        remote = Artifacts()
        with tempfile.NamedTemporaryFile() as f:
            text, meta = adapter['store_stream'](f.name, remote)
            self.assertEqual((text, meta['chunks'], meta['size']), ('', [], 0))
            data = b'\xffabc' * 65537
            f.write(data)
            f.flush()
            text, meta = adapter['store_stream'](f.name, remote)
            self.assertEqual(text, data[:4096].decode('utf-8', 'replace'))
            self.assertTrue(meta['preview_truncated'])
            self.assertEqual(meta['sha256'], hashlib.sha256(data).hexdigest())
            self.assertEqual(len(meta['chunks']), 2)
            self.assertEqual(b''.join(remote.parts), data)
