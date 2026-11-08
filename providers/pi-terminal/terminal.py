"""BiomeRTE adapter for the installed Linux terminal supervisor (Python 3.4+)."""
import base64
import hashlib
import json
import os
import socket
import sys
import uuid

CONTRACT = 'host.terminal.run:1'
DEFAULT_SLOTS = 16
OUTPUTS = {'exit_code': 'int', 'stdout': 'str', 'stderr': 'str', 'streams': 'obj',
           'evidence': 'obj'}


def terminal_usage(profile):
    exe = profile['executable']
    return {
        'before': 'Acquire one terminal slot per independent session. Commands on the same lease run serially.',
        'parameters': {'argv': 'An array of 1 to 32 strings, each at most 4096 characters. '
                               'Start with the enrolled executable ' + exe + '. Pass -c and one script string for shell syntax.'},
        'example_inputs': {'argv': [exe, '-c', 'hostname']},
        'read_result': 'On succeeded, read result.exit_code and result.stdout/stderr. For hostname, stdout is the selected host name. '
                       'The text previews cover the first 4096 bytes per stream. For complete output use artifact.get with each '
                       'result.streams.stdout or stderr chunk sha256, base64-decode in order and verify the stream digest.',
        'errors': 'Exit 126 with evidence.executed=false means the argv or executable digest check rejected the command. '
                  'An executed program can return a nonzero exit code. Job limits are opt-in on the host.'
    }


def terminal_pool(resource, profile, adapter_argv, slots=DEFAULT_SLOTS):
    if type(slots) is not int or slots < 1:
        raise ValueError('Terminal slots must be a positive integer')
    offer = {'contract': CONTRACT, 'resource': resource, 'service': 'terminal',
             'capacity': slots, 'argv': adapter_argv, 'usage': terminal_usage(profile)}
    return {'offers': [offer], 'parallelism': slots}


def read_json(path):
    with open(path) as stream:
        return json.load(stream)


def check_argv(argv, profile):
    if not isinstance(argv, list) or not 1 <= len(argv) <= 32:
        raise ValueError('Expected 1 to 32 arguments')
    if any(not isinstance(a, str) or len(a) > 4096 or '\x00' in a for a in argv):
        raise ValueError('Arguments must be strings of at most 4096 characters without NUL')
    exe = profile['executable']
    if not os.path.isabs(argv[0]) or os.path.realpath(argv[0]) != exe:
        raise ValueError('First argument must select the installed terminal executable')
    with open(exe, 'rb') as stream:
        if hashlib.sha256(stream.read()).hexdigest() != profile['executable_sha256']:
            raise ValueError('Installed executable differs from the enrolled digest')
    return [exe] + argv[1:]


def store_stream(path, remote, chunk_bytes=256 * 1024):
    """Keep small replies readable and make every original byte retrievable."""
    whole = hashlib.sha256()
    chunks = []
    preview = b''
    size = 0
    with open(path, 'rb') as stream:
        while True:
            block = stream.read(chunk_bytes)
            if not block:
                break
            whole.update(block)
            size += len(block)
            if not chunks:
                preview = block[:4096]
            sha = hashlib.sha256(block).hexdigest()
            receipt = remote.call('artifact.put', {'data': base64.b64encode(block).decode('ascii'), 'transient': True},
                                  'terminal-transient-chunk-' + str(uuid.uuid4()))
            if receipt != {'sha256': sha, 'size': len(block)}:
                raise IOError('Artifact receipt mismatch')
            chunks.append(receipt)
    return preview.decode('utf-8', 'replace'), {
        'size': size, 'sha256': whole.hexdigest(), 'chunks': chunks,
        'preview_truncated': size > len(preview)}


def execute(cfg, home, request):
    # Reuse the installed Jessie-compatible native custodian, with a private
    # journal. No Channel Bus sessions, job rows or credentials are used here.
    sys.path.insert(0, cfg['supervisor_dir'])
    from sdk import Journal, monotonic
    from terminal_supervisor import execute as supervise
    from terminal_limits import read_limits
    from terminal_retention import lookup, cache, trim, encoded, initialize
    from biomerte.worker import Remote
    from agent import inventory
    profile = cfg['terminal_profile']
    try:
        argv = check_argv(request['inputs']['argv'], profile)
    except ValueError as exc:
        # Rejection before launch is a known outcome, safe for the next lease.
        return {'exit_code': 126, 'stdout': '', 'stderr': str(exc), 'streams': {},
                'evidence': {'executed': False, 'quiescent': True, 'reason': 'argv_rejected'}}

    op = request['operation']
    work = os.path.join(home, 'jobs', hashlib.sha256(op.encode()).hexdigest())
    os.makedirs(work, mode=0o700, exist_ok=True)
    path = os.path.join(home, 'terminal.sqlite')
    journal = Journal(path)
    try:
        limits = read_limits(home)
        if 'memory_bytes' in profile.get('limits', {}):
            memory = profile['limits']['memory_bytes']
            limits['memory_bytes'] = min(memory, limits.get('memory_bytes', memory))
        body = {'argv': argv, 'limits': limits, 'workspace': work}
        duration = limits.get('duration_seconds')
        lease = None if duration is None else monotonic() + duration
        initialize(journal.db)
        with journal.db:
            inserted = journal.db.execute('INSERT OR IGNORE INTO jobs VALUES(?,?,?,?,?,NULL)',
                               (op, json.dumps(body), 'queued', None, lease)).rowcount
            if inserted:
                journal.db.execute('INSERT INTO terminal_retention_eligible VALUES(?)', (op,))
        eligible = journal.db.execute('SELECT 1 FROM terminal_retention_eligible WHERE id=?', (op,)).fetchone() is not None
        existing = journal.db.execute('SELECT body FROM jobs WHERE id=?', (op,)).fetchone()
        recorded = json.loads(existing[0])
        if recorded.get('retired'):
            identity = hashlib.sha256(encoded([argv, work]).encode()).hexdigest()
            if recorded['request_digest'] != identity:
                raise RuntimeError('Terminal operation identity conflict')
        elif recorded['argv'] != argv or recorded['workspace'] != work:
            raise RuntimeError('Terminal operation identity conflict')
        found, uploaded = lookup(journal.db, op)
        if found:
            return uploaded
        if recorded.get('retired'):
            raise RuntimeError('Terminal result expired; operation cannot execute again')
        supervise(path, op)
        row = journal.db.execute('SELECT body,state,result FROM jobs WHERE id=?', (op,)).fetchone()
        if row[1] != 'finished':
            raise RuntimeError('Unresolved terminal operation; inspect the supervisor journal')
        result = json.loads(row[2])
    finally:
        journal.db.close()
    if not result.get('quiescent') or result.get('reason') == 'supervisor_error':
        raise RuntimeError('Inspect the terminal supervisor result before resolving this effect')
    node = read_json(os.path.join(home, 'node.json'))
    remote = Remote(cfg['url'], cfg['site'], node['node'], os.path.join(home, 'node.key'))
    out = {'exit_code': result['exit_code'], 'streams': {}}
    for name in ('stdout', 'stderr'):
        out[name], out['streams'][name] = store_stream(result[name], remote)
    out['evidence'] = {k: result[k] for k in ('exit_code', 'duration_seconds', 'reason',
                                              'process_identity', 'quiescent')}
    out['evidence'].update(executed=True, host=socket.gethostname(), executor_identity=inventory())
    journal = Journal(path)
    try:
        if eligible:
            cache(journal.db, op, out, {name: result[name] for name in ('stdout', 'stderr')})
        try:
            trim(journal.db, home)
        except (OSError, ValueError, RuntimeError):
            # Diagnostic cleanup must not turn a completed effect into an
            # uncertain execution. Durable ACK cleanup will retry later.
            pass
    finally:
        journal.db.close()
    return out


def acknowledge_result(manifest, request):
    import sqlite3
    from terminal_retention import acknowledge, trim
    home = os.path.dirname(os.path.abspath(manifest))
    db = sqlite3.connect(os.path.join(home, 'terminal.sqlite'), timeout=30)
    try:
        from terminal_retention import initialize
        initialize(db)
        if not db.execute('SELECT 1 FROM terminal_retention_eligible WHERE id=?', (request['operation'],)).fetchone():
            return {'acknowledged': True, 'legacy_preserved': True}
        acknowledge(db, request['operation'], request['result_digest'])
        trim(db, home)
    finally:
        db.close()
    return {'acknowledged': True}


if __name__ == '__main__':
    if sys.argv[1] == '--acknowledge':
        json.dump(acknowledge_result(sys.argv[2], json.load(sys.stdin)), sys.stdout)
    else:
        manifest = os.path.abspath(sys.argv[1])
        json.dump(execute(read_json(manifest), os.path.dirname(manifest), json.load(sys.stdin)), sys.stdout)
