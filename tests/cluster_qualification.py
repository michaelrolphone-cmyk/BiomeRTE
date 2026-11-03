"""Run against the dedicated disposable deploy/etcd-lab.yml cluster only."""
import json
import os
import subprocess
import tempfile
import time
from urllib.request import Request, urlopen
from biomerte.app import Application, initialize
from biomerte.store import uid, Fault


def main():
    urls = ['http://127.0.0.1:%d' % p for p in (23791, 23792, 23793)]
    compose = ['docker', 'compose', '-f', 'deploy/etcd-lab.yml']
    site = 'test-' + uid()
    names = ['a', 'b', 'c']
    with tempfile.TemporaryDirectory() as root:
        nodes = []
        for i in range(3):
            d = os.path.join(root, str(i))
            cfg = initialize(d, site)
            cfg['etcd'] = {'endpoints': urls, 'timeout': 1}
            with open(os.path.join(d, 'node.json'), 'w') as f:
                json.dump(cfg, f)
            nodes.append(Application(d))

        def eventually(fn):
            for attempt in range(30):
                try:
                    return fn()
                except (Fault, OSError) as err:
                    if attempt == 29:
                        raise
                    time.sleep(.3)
        a, b, c = nodes
        eventually(lambda: a.call('work.create', {
                   'kind': 'task', 'id': 'first', 'title': 'survives leader'}, 'one'))
        assert b.call('work.list')[0]['id'] == 'first'
        status = a.call('cluster.status')
        assert len(status['members']) == 3
        # The provider applies an effect once. Its *reply* will be delivered only after failover.
        a.call('cap.define', {'name': 'lab.effect', 'inputs': {},
               'outputs': {'done': 'bool'}}, 'contract')
        a.call('cap.offer', {'provider': 'probe',
               'contract': 'lab.effect:1', 'resource': 'relay'}, 'offer')
        lease = a.call('cap.acquire', {'task': 'first', 'contract': 'lab.effect:1'}, 'lease')
        b.call('cap.fenced', {'provider': 'probe',
               'lease': lease['id'], 'generation': lease['generation']}, 'fence')
        a.call('cap.invoke', {'lease': lease['id'],
               'inputs': {}, 'operation': 'switch-on'}, 'dispatch')
        claim = {'provider': 'probe', 'operation': 'switch-on', 'generation': lease['generation']}
        assert b.call('provider.claim', claim, 'claim')['state'] == 'claimed'
        device_ledger = {'switch-on': {'done': True}}

        statuses = [json.loads(urlopen(Request(u + '/v3/maintenance/status', b'{}',
                               {'Content-Type': 'application/json'}), timeout=3).read().decode()) for u in urls]
        leader = next(i for i, s in enumerate(statuses) if str(
            s['leader']) == str(s['header']['member_id']))
        other = (leader + 1) % 3
        try:
            subprocess.check_call(compose + ['stop', names[leader]])
            eventually(lambda: b.call('work.create', {
                       'kind': 'task', 'id': 'second', 'title': 'after election'}, 'two'))
            assert len(c.call('work.list')) == 2
            pending = c.call('provider.poll', {'provider': 'probe'})['actions']
            assert len(pending) == 1 and pending[0]['id'] == 'switch-on'
            peer_reply = dict(claim, state='succeeded', result=device_ledger[pending[0]['id']])
            c.call('provider.result', peer_reply, 'reply')
            assert b.call('provider.result', peer_reply, 'reply')['result'] == {'done': True}
            assert not c.call('provider.poll', {'provider': 'probe'})['actions']

            subprocess.check_call(compose + ['stop', names[other]])
            try:
                c.call('work.create', {'kind': 'task',
                       'id': 'forbidden', 'title': 'minority'}, 'three')
            except Fault:
                pass
            else:
                raise AssertionError('minority acknowledged a shared write')
            assert c.call('work.create', {'kind': 'task', 'id': 'local',
                          'title': 'field work'}, 'local', 'local')['id'] == 'local'
            assert len(c.call('work.list', realm='site', cached=True)) == 2
        finally:
            subprocess.check_call(compose + ['start', names[leader], names[other]])
        rows = eventually(lambda: a.call('work.list'))
        assert {r['id'] for r in rows} == {'first', 'second'}
        print('Three voters: leader loss, majority takeover, minority rejection, independent local work, catch-up passed.')


if __name__ == '__main__':
    main()
