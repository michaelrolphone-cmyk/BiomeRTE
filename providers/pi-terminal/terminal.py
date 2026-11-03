"""BiomeRTE adapter for the installed Linux terminal supervisor (Python 3.4+)."""
import base64
import hashlib
import json
import os
import socket
import sys

CONTRACT = 'host.terminal.run:1'
OUTPUTS = {'exit_code': 'int', 'stdout': 'str', 'stderr': 'str', 'streams': 'obj',
           'evidence': 'obj'}


def terminal_usage(profile):
    exe = profile['executable']
    limits = profile['limits']
    return {
        'before': 'Acquire one terminal slot per independent session. Commands on the same lease run serially.',
        'parameters': {'argv': 'An array of 1 to 32 strings, each at most 4096 characters. '
                               'Start with the enrolled executable ' + exe + '. Pass -c and one script string for shell syntax.'},
        'example_inputs': {'argv': [exe, '-c', 'hostname']},
        'read_result': 'On succeeded, read result.exit_code and result.stdout/stderr. For hostname, stdout is the Pi host name. '
                       'The text previews cover the first 4096 bytes per stream. For complete output use artifact.get with each '
                       'result.streams.stdout or stderr chunk sha256, base64-decode in order and verify the stream digest.',
        'errors': 'Exit 126 with evidence.executed=false means the argv or executable digest check rejected the command. '
                  'An executed program can return a nonzero exit code. This profile allows %d seconds and %d bytes per output file.'
                  % (limits['duration_seconds'], limits['output_bytes'])
    }


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


def store_stream(path, remote):
    """Keep small replies readable and make every original byte retrievable."""
    whole = hashlib.sha256()
    chunks = []
    preview = b''
    size = 0
    with open(path, 'rb') as stream:
        while True:
            block = stream.read(256 * 1024)
            if not block:
                break
            whole.update(block)
            size += len(block)
            if not chunks:
                preview = block[:4096]
            sha = hashlib.sha256(block).hexdigest()
            receipt = remote.call('artifact.put', {'data': base64.b64encode(block).decode('ascii')},
                                  'terminal-chunk-' + sha)
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
    from supervisor import execute as supervise
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
        body = {'argv': argv, 'limits': profile['limits'], 'workspace': work}
        lease = monotonic() + profile['limits']['duration_seconds']
        with journal.db:
            journal.db.execute('INSERT OR IGNORE INTO jobs VALUES(?,?,?,?,?,NULL)',
                               (op, json.dumps(body), 'queued', None, lease))
        existing = journal.db.execute('SELECT body FROM jobs WHERE id=?', (op,)).fetchone()
        if json.loads(existing[0]) != body:
            raise RuntimeError('Terminal operation identity conflict')
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
    return out


if __name__ == '__main__':
    manifest = os.path.abspath(sys.argv[1])
    json.dump(execute(read_json(manifest), os.path.dirname(manifest), json.load(sys.stdin)), sys.stdout)
