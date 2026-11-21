"""Executed inside a run-owned workspace. No host paths, network or timeouts.

Build in a disposable copy, bind evidence to all tracked/nonignored source bytes,
and commit only the exact tree previously tested and reviewed.
"""
import base64, hashlib, json, os, pathlib, shutil, subprocess, sys, tempfile

def git(root, *args):
    p = subprocess.run(['git', *args], cwd=root, capture_output=True)
    if p.returncode:
        raise ValueError(p.stderr.decode(errors='replace'))
    return p.stdout

def snapshot(root):
    root = pathlib.Path(root).resolve()
    names = sorted(set(git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').split(b'\0')) - {b''})
    files = {}
    for raw in names:
        name = os.fsdecode(raw); path = root / name
        if path.is_symlink():
            files[name] = {'kind': 'symlink', 'target': os.readlink(path)}
        elif path.is_file():
            if not path.resolve().is_relative_to(root): raise ValueError('Source escapes repository')
            files[name] = {'kind': 'file', 'executable': bool(path.stat().st_mode & 0o111), 'base64': base64.b64encode(path.read_bytes()).decode()}
        elif path.exists(): raise ValueError('Submodules require an explicit independently pinned source set')
        # Removed tracked files are represented by absence, and staged by git add -A.
    if not files: raise ValueError('No source files')
    rev = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'source_revision': rev, 'source_files': files, 'base_commit': git(root, 'rev-parse', 'HEAD').decode().strip()}

def run(spec):
    root = pathlib.Path(spec['root']).resolve()
    if not root.is_relative_to(pathlib.Path('/workspace')) and not spec.get('_test_root'):
        raise ValueError('Source must be inside the workspace volume')
    evidence = snapshot(root)
    evidence['environment']=spec.get('environment',{})
    mode = spec.get('mode', 'test')
    if mode == 'snapshot': return evidence
    if mode == 'commit':
        if evidence['source_revision'] != spec['expected_revision']: raise ValueError('Source changed since tests/review')
        git(root, 'add', '--all')
        if git(root, 'diff', '--cached', '--name-only'):
            git(root, '-c', 'user.name=Biome Agent', '-c', 'user.email=biome-agent@biome.local', 'commit', '-m', spec['message'])
        after = snapshot(root)
        if after['source_revision'] != evidence['source_revision']: raise ValueError('Commit changed reviewed source')
        return dict(after, commit=git(root, 'rev-parse', 'HEAD').decode().strip())
    if mode != 'test' or not spec.get('commands'): raise ValueError('Explicit build/test commands required')
    evidence.update(commands=[], tests_passed=True)
    with tempfile.TemporaryDirectory(prefix='biome-build-') as directory:
        shutil.copytree(root, directory, dirs_exist_ok=True, symlinks=True)
        for argv in spec['commands']:
            if not isinstance(argv, list) or not argv or not all(type(v) is str for v in argv): raise ValueError('Commands must be argv arrays')
            p = subprocess.run(argv, cwd=directory, capture_output=True, text=True)
            evidence['commands'].append({'argv': argv, 'exit_code': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr})
            if p.returncode: evidence['tests_passed'] = False; break
    if snapshot(root)['source_revision'] != evidence['source_revision']: raise ValueError('Source changed during build')
    return evidence

if __name__ == '__main__':
    result = run(json.loads(sys.argv[1])); print(json.dumps(result))
    # Test failures are measured data, not an interrupted provider operation.
    # The workflow inspects tests_passed and each command exit code.
