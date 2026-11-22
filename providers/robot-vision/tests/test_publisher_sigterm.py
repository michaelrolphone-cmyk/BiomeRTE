"""Exercise the real publisher signal handler, signed authority and durable worker together."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / 'providers/robot-vision'), str(ROOT)]
import biome_provider as adapter
from biomerte.app import Application, initialize
from biomerte.identity import fingerprint, generate, sign
from biomerte.store import uid


def run_publisher(folder):
    state, authority = folder / 'worker', folder / 'authority'
    state.mkdir()
    initialize(str(authority), 'test-site')
    app = Application(str(authority))
    private = str(folder / 'publisher.key')
    public = generate(private)
    actor = fingerprint(public)
    queries = {f'query.{i}:1': dict(name=f'query.{i}', version=1, inputs={}, outputs={})
               for i in range(47)}
    permissions = ['cap.offer','cap.define','provider.poll','resource:camera','resource:camera.rear',
                   'offer:robot.vision.stream:1', *('offer:' + contract for contract in queries)]
    app.call('peer.trust',dict(public_key=public,label='publisher',scopes=permissions),uid())
    app.call('cap.define',dict(name='independent',inputs={},outputs={}),uid())
    app.call('cap.offer',dict(provider=actor,contract='independent:1',resource='camera',service='other'),uid())
    cfg = dict(state=str(state), repo=str(ROOT), provider=actor, site='test-site', slots=1,
               socket='unused', resource='camera', python=sys.executable, poll_seconds=.05)
    class LocalSignedAuthority:
        def __init__(self,cfg):
            self.node, self.site, self.realm = actor, cfg['site'], 'site'
            self.ready = set()
        def call(self,name,args,rid=None):
            rid = rid or uid()
            body = dict(site=self.site, realm=self.realm, operation=name, arguments=args, id=rid)
            result = app.call(name,args,rid,auth=dict(actor=actor,signature=sign(private,body)))
            if name == 'cap.offer' and args['contract'] in queries:
                if args['state'] == 'ready':
                    self.ready.add(args['contract'])
                    if len(self.ready) == len(queries):
                        (folder / 'ready').touch()
                else:
                    with (folder / 'single-offer-withdrawals').open('a') as log:
                        print(args['contract'],file=log)
            if name == 'cap.withdraw':
                with (folder / 'withdrawals').open('a') as log:
                    print(json.dumps(result),file=log)
            return result
    class HealthyReceiver:
        def __init__(self,*args): pass
        def query(self,*args):
            return dict(state='running',capabilities=list(queries),
                        streaming=dict(available=True,lanes=['vision','map'],rear_enabled=True))
    with patch.object(adapter,'Remote',LocalSignedAuthority), patch.object(adapter,'VisionClient',HealthyReceiver), \
            patch.object(adapter,'CONTRACTS',queries):
        adapter.serve(cfg)


class PublisherSigterm(unittest.TestCase):
    def test_sigterm_with_47_offers_uses_one_signed_withdrawal_and_preserves_other_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            with (folder / 'publisher.log').open('w+') as log:
                proc = subprocess.Popen([sys.executable, __file__, 'serve-fixture', tmp],stdout=log,stderr=log)
                try:
                    deadline = time.monotonic() + 35
                    while time.monotonic() < deadline and proc.poll() is None:
                        if (folder / 'ready').exists():
                            break
                        time.sleep(.05)
                    else:
                        log.seek(0)
                        self.fail('Publisher failed to register the fixture queries: ' + log.read()[-2000:])
                    os.kill(proc.pid, signal.SIGTERM)
                    self.assertEqual(proc.wait(12), 0)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()
            singles = folder / 'single-offer-withdrawals'
            self.assertEqual(singles.read_text().splitlines() if singles.exists() else [], [])
            batches = [json.loads(line) for line in (folder / 'withdrawals').read_text().splitlines()]
            self.assertEqual(len(batches), 1)
            self.assertEqual(len(batches[0]['withdrawn']), 47)
            self.assertFalse(batches[0]['superseded'])
            app = Application(str(folder / 'authority'))
            offers = app.call('cap.discover',{},uid())['offers']
            self.assertEqual(sum(v['state']=='offline' and v['contract'].startswith('query.') for v in offers),47)
            self.assertEqual(next(v['state'] for v in offers if v['contract']=='independent:1'),'ready')


if __name__ == '__main__' and len(sys.argv) == 3 and sys.argv[1] == 'serve-fixture':
    run_publisher(Path(sys.argv[2]))
