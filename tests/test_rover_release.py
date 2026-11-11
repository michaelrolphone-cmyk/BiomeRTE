import importlib.util
import json
import pathlib
import subprocess
import tempfile
import unittest
import hashlib

spec = importlib.util.spec_from_file_location('build_rover_release', pathlib.Path(__file__).parents[1]/'deploy/build_rover_release.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class ReleaseTests(unittest.TestCase):
    def test_release_uses_committed_pins_and_ignores_dirty_checkout(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td); repo = root/'repo';repo.mkdir()
            def git(*args):
                return subprocess.check_output(['git','-C',str(repo),*args],stderr=subprocess.DEVNULL).decode().strip()
            git('init');git('config','user.name','Release Test');git('config','user.email','release@example.invalid')
            (repo/'engine.py').write_text('recorded baseline\n');git('add','.');git('commit','-m','base')
            base = git('rev-parse','HEAD')
            (repo/'engine.py').write_text('preserved production\n');git('add','.');git('commit','-m','production')
            production = git('rev-parse','HEAD')
            profile = dict(schema_version=1,source_commit=base,overrides=[dict(path='engine.py',commit=production,
                           sha256=hashlib.sha256(b'preserved production\n').hexdigest())])
            (repo/'profile.json').write_text(json.dumps(profile));git('add','.');git('commit','-m','profile')
            release = git('rev-parse','HEAD')
            (repo/'engine.py').write_text('unrecorded edit\n')
            result = builder.assemble(repo,release,root/'release','profile.json')
            self.assertEqual((root/'release/engine.py').read_text(),'preserved production\n')
            self.assertEqual(result['release_commit'],release)
            self.assertEqual(result['python_sha256']['engine.py'],profile['overrides'][0]['sha256'])
            profile['overrides'][0]['sha256']='0'*64
            (repo/'profile.json').write_text(json.dumps(profile));git('add','profile.json');git('commit','-m','bad pin')
            with self.assertRaisesRegex(ValueError,'hash mismatch'):
                builder.assemble(repo,git('rev-parse','HEAD'),root/'bad-release','profile.json')
            self.assertFalse((root/'bad-release').exists())
