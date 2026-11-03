"""Run on the commissioned Pi. Holds 16 leases briefly and cleans up its own jobs."""
import json
import os
import shutil
import tempfile
import time
import uuid
from biomerte.app import read_json
from biomerte.worker import Remote


def check(home):
    config = read_json(os.path.join(home, 'node.json'))
    remote = Remote('http://127.0.0.1:%d' % config['port'], config['site'],
                    config['node'], None, token=config['admin_token'])
    call = remote.call
    contract = 'host.terminal.run:1'
    offers = call('cap.discover', {'contract':contract})['offers']
    resource = next(o['resource'] for o in offers if o['state']=='ready' and o['capacity']==16)
    tag = 'pool-qa-' + uuid.uuid4().hex[:12]
    work = tempfile.mkdtemp(prefix=tag)
    gate = os.path.join(work, 'go')
    leases, actions = [], []

    def wait(read, accept, timeout=100):
        stop = time.monotonic() + timeout
        while True:
            result = read()
            if accept(result):
                return result
            if time.monotonic() >= stop:
                raise RuntimeError('Timed out waiting for ' + str(result)[:200])
            time.sleep(.35)

    def acquire(i):
        args = {'task':tag+'-'+str(i), 'resource':resource, 'contract':contract}
        if i == 16:
            args['reply_to'] = tag
        lease = call('cap.acquire', args)
        leases.append(lease['id'])
        return lease

    def by_id(key):
        return call('cap.lease', {'lease':key})

    def status(key):
        return call('cap.status', {'operation':key})

    try:
        for i in range(16):
            acquire(i)
        wait(lambda:call('cap.leases',{}),
             lambda rows:sum(l['id'] in leases and l['state']=='bound' for l in rows)==16)
        extra = acquire(16)
        assert extra['state']=='queued'
        command = ('read start rest </proc/uptime; echo $$ > "$1/$2"; '
                   'for n in {1..600}; do [ -e "$1/go" ] && break; sleep .25; done; '
                   '[ -e "$1/go" ] || exit 124; read end rest </proc/uptime; echo "$$ $start $end"')
        for i, lease in enumerate(leases[:16]):
            op = tag+'-op-'+str(i)
            call('cap.invoke', {'operation':op,'lease':lease,
                                'inputs':{'argv':['/bin/bash','-c',command,'bash',work,str(i)]}}, op)
            actions.append(op)
        wait(lambda:len(os.listdir(work)), lambda count:count==16)
        with open(gate, 'w'):
            pass
        results = [wait(lambda:status(op),lambda a:a['state'] not in ('queued','claimed')) for op in actions]
        times = []
        for a in results:
            assert a['state']=='succeeded' and a['result']['exit_code']==0
            pid, start, end = a['result']['stdout'].split()
            times.append((int(pid), float(start), float(end)))
        assert len(set(t[0] for t in times))==16
        overlap = min(t[2] for t in times) - max(t[1] for t in times)
        assert overlap >= 0
        call('cap.release',{'lease':leases[0]})
        grant = wait(lambda:by_id(extra['id']),lambda l:l['state']=='bound')
        notices = call('bus.inbox', {'address':tag})
        assert [m['body']['id'] for m in notices]==[extra['id']]
        op = tag+'-docker'
        call('cap.invoke', {'operation':op,'lease':grant['id'],'inputs':{'argv':['/bin/bash','-c',
            "command -v docker && docker --version || echo 'docker missing'"]}}, op)
        actions.append(op)
        done = wait(lambda:status(op), lambda a:a['state']=='succeeded')
        return {'resource':resource,'tag':tag,'parallel_processes':16,'overlap_seconds':overlap,
                'seventeenth_initial':extra['state'],'seventeenth_final':grant['state'],
                'matched_notifications':len(notices),'docker':done['result']['stdout'].strip()}
    finally:
        with open(gate,'a'):
            pass
        for op in actions:
            wait(lambda:status(op), lambda a:a['state'] not in ('queued','claimed'), 45)
        for lease in reversed(leases):
            call('cap.release',{'lease':lease})
        shutil.rmtree(work)


if __name__=='__main__':
    import sys
    print(json.dumps(check(sys.argv[1])))
