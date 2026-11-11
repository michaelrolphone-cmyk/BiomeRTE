"""Assemble a reproducible Rover runtime from committed source pins only."""
import argparse
import hashlib
import json
import pathlib
import re
import subprocess


def assemble(repository, release_commit, target, profile='deploy/profiles/mac-rover-20261111.json'):
    repository, target = pathlib.Path(repository), pathlib.Path(target)
    def sha(value):
        if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{40}', value):
            raise ValueError('An exact Git commit is required')
        return value
    def read(commit, path):
        p = pathlib.PurePosixPath(path)
        if p.is_absolute() or '..' in p.parts:
            raise ValueError('Release source path must stay inside the repository')
        return subprocess.check_output(['git', '-C', str(repository), 'show', sha(commit)+':'+path])
    manifest = json.loads(read(release_commit, profile))
    if manifest.get('schema_version') != 1:
        raise ValueError('Unknown release profile')
    base = sha(manifest['source_commit'])
    overrides = []
    for row in manifest['overrides']:
        data = read(row['commit'], row['path'])
        if hashlib.sha256(data).hexdigest() != row['sha256']:
            raise ValueError('Pinned release source hash mismatch: '+row['path'])
        overrides.append((row['path'], data))
    target.mkdir(parents=True, mode=0o700)
    archive = subprocess.Popen(['git', '-C', str(repository), 'archive', base], stdout=subprocess.PIPE)
    try:
        subprocess.run(['tar', '-x', '-C', str(target)], stdin=archive.stdout, check=True)
    finally:
        archive.stdout.close()
        code = archive.wait()
    if code:
        raise RuntimeError('Committed release archive failed')
    for path, data in overrides:
        (target/path).write_bytes(data)
    hashes = {str(p.relative_to(target)):hashlib.sha256(p.read_bytes()).hexdigest()
              for p in target.rglob('*.py')}
    provenance = dict(release_commit=release_commit, profile=profile,
                      source_profile=manifest, python_sha256=hashes)
    (target/'deployment-provenance.json').write_text(json.dumps(provenance, indent=2)+'\n')
    return provenance


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repository')
    parser.add_argument('release_commit')
    parser.add_argument('target')
    args = parser.parse_args()
    result = assemble(args.repository, args.release_commit, args.target)
    print(json.dumps(dict(release_commit=result['release_commit'], files=len(result['python_sha256']))))
