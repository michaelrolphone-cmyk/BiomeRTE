"""Crash-safe experiment metadata and an exclusive local run lock."""
import contextlib
import fcntl
import hashlib
import json
import os
import tempfile
from pathlib import Path


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def source_identity():
    root=Path(__file__).resolve().parents[1]
    return digest({str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
                   for folder in ('activation_field','experiments')
                   for p in sorted((root/folder).rglob('*.py'))})


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def run_lock(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / '.run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('Experiment already running') from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def prepare(root, config, data, baseline_hash, resume):
    """Resume only the identical frozen experiment, never mix changed results."""
    root = Path(root)
    identity = dict(schema_version=2, config_hash=digest(config),source_hash=source_identity(),
                    corpus_hash=data['sha256'], baseline_hash=baseline_hash)
    path = root / 'identity.json'
    if path.exists():
        if not resume:
            raise ValueError('Existing experiment requires explicit --resume')
        if json.loads(path.read_text()) != identity:
            raise ValueError('Experiment identity changed; use a new output directory')
    else:
        if (root / 'results.json').exists():
            raise ValueError('Legacy results lack pinned identity; preserve them and use a new directory')
        atomic_json(root / 'corpus.json', data)
        atomic_json(root / 'config.json', config)
        atomic_json(path, identity)
    saved = json.loads((root / 'corpus.json').read_text())
    actual = digest({k: v for k, v in saved.items() if k != 'sha256'})
    if actual != saved['sha256'] or saved['sha256'] != data['sha256']:
        raise ValueError('Frozen corpus checksum mismatch')
    return identity
