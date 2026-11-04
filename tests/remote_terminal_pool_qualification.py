"""Live controller-side test of a specified remote terminal's 16 slots and queued notification."""
import base64
import hashlib
import json
import os
import sys
import time
import uuid

from biomerte.app import read_json
from biomerte.worker import Remote


def check(home, resource):
    owner = read_json(os.path.join(home, 'node.json'))
    remote = Remote('http://127.0.0.1:%d' % owner['port'], owner['site'], owner['node'], None,
                    token=owner['admin_token'])
    call = remote.call
    tag = 'terminal-parity-' + uuid.uuid4().hex[:12]
    contract = 'host.terminal.run:1'
    leases, actions = [], []

    def wait(fn, predicate, timeout=480):
        end = time.monotonic() + timeout
        while True:
            v = fn()
            if predicate(v):
                return v
            if time.monotonic() >= end:
                raise RuntimeError('Timed out: ' + str(v)[:150])
            time.sleep(.7)

    def result(op):
        a = wait(lambda: call('cap.status', {'operation': op}),
                 lambda v: v['state'] in ('succeeded', 'failed', 'uncertain'))
        if a['state'] != 'succeeded' or a['result']['exit_code'] != 0:
            raise RuntimeError('Remote command failed: ' + str(a.get('result', a))[:300])
        return a['result']

    def submit(lease, suffix, argv):
        op = tag + '-' + suffix
        call('cap.invoke', {'operation': op, 'lease': lease, 'inputs': {'argv': argv}}, op)
        actions.append(op)
        return op

    offers = call('cap.discover', {'contract': contract})['offers']
    offer = next(o for o in offers if o['resource'] == resource)
    assert offer['state'] == 'ready' and offer['capacity'] == 16
    try:
        for i in range(16):
            lease = call('cap.acquire', dict(contract=contract, task=tag+'-'+str(i), resource=resource))
            leases.append(lease['id'])
        wait(lambda: call('cap.leases', {}),
             lambda rows: sum(l['id'] in leases and l['state'] == 'bound' for l in rows) == 16)
        extra = call('cap.acquire', dict(contract=contract, task=tag+'-17', resource=resource,
                                         enqueue=True, reply_to=tag))
        leases.append(extra['id'])
        assert extra['state'] == 'queued'
        work = result(submit(leases[0], 'dir', ['/bin/bash', '-c', 'mktemp -d /tmp/biome-terminal-qa.XXXXXXXX']))['stdout'].strip()
        assert work.startswith('/tmp/biome-terminal-qa.') and '/' not in work[23:]
        code = """import json,os,socket,sys,time
directory,slot=sys.argv[1:]
start=time.monotonic()
open(os.path.join(directory,slot),'x').close()
while len(os.listdir(directory))<16 and time.monotonic()-start<320:time.sleep(.1)
assert len(os.listdir(directory))==16
time.sleep(2)
print(json.dumps([os.getpid(),start,time.monotonic(),socket.gethostname()]))"""
        ops = [submit(lease, 'parallel-'+str(i), ['/bin/bash', '-c', 'exec python3 -c "$1" "$2" "$3"',
                                                  'qa', code, work, str(i)]) for i, lease in enumerate(leases[:16])]
        runs = [json.loads(result(op)['stdout']) for op in ops]
        overlap = min(row[2] for row in runs) - max(row[1] for row in runs)
        assert len(set(row[0] for row in runs)) == 16 and overlap > 0
        call('cap.release', {'lease': leases[0]})
        waiting = wait(lambda: call('cap.lease', {'lease': extra['id']}), lambda l: l['state'] == 'bound')
        notices = call('bus.inbox', {'address': tag})
        matched = [m for m in notices if m['body']['id'] == extra['id'] and m['body']['type'] == 'lease.available']
        assert len(matched) == 1
        # Confirm the promoted waiter can execute and retrieve bytes beyond the 4 KiB preview.
        follow = result(submit(waiting['id'], 'stream', ['/bin/bash', '-c',
                        "python3 -c 'import sys; sys.stdout.buffer.write(bytes(range(256))*230)'"]))
        chunks = follow['streams']['stdout']['chunks']
        data = b''.join(base64.b64decode(call('artifact.get', {'sha256': r['sha256']})['data']) for r in chunks)
        assert data == bytes(range(256))*230 and len(chunks) == 2
        out = dict(resource=resource, host=runs[0][3], simultaneous_processes=len(runs), overlap=round(overlap,3),
                   extra_initial=extra['state'], extra_final=waiting['state'], notifications=len(matched),
                   artifact_bytes=len(data), chunks=len(chunks), tag=tag)
        return out
    finally:
        if 'work' in locals() and len(leases)>1:
            try:
                result(submit(leases[1], 'cleanup', ['/bin/bash','-c',
                             'case "$1" in /tmp/biome-terminal-qa.*) rm -rf -- "$1";; *) exit 1;; esac','qa',work]))
            except Exception as exc:
                print('QA workspace cleanup: ' + str(exc)[:160], file=sys.stderr)
        for lease in reversed(leases):
            receipt = call('cap.release', {'lease':lease})
            if receipt['state'] != 'released':
                print('Lease requiring reconciliation: '+lease,file=sys.stderr)


if __name__ == '__main__':
    print(json.dumps(check(sys.argv[1], sys.argv[2])))
