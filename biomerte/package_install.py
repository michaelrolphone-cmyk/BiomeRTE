"""Explicit installation of operator-reviewed provider code. Never auto-run bus payloads."""
import hashlib
import os
import json
import subprocess
import sys
from .store import Fault, encode, mkdir, un64, uid


def install(app, package_id, resource, outpath):
    p = app.call('package.get', {'id': package_id})
    raw = un64(app.call('artifact.get', {'sha256': p['artifact']})['data'])
    if hashlib.sha256(raw).hexdigest() != p['artifact']:
        raise Fault('Corrupt adapter artifact')
    dest = os.path.join(app.directory, 'packages', p['id'])
    mkdir(dest)
    path = os.path.join(dest, 'adapter.py')
    with open(path, 'wb') as f:
        f.write(raw)
    argv = [sys.executable, path]
    for vector in p['probes']:
        child = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            stdout, unused = child.communicate(
                encode({'operation': uid(), 'inputs': vector['inputs']}).encode(), timeout=30)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate()
            raise Fault('Package probe timed out')
        if child.returncode or json.loads(stdout.decode()) != vector['expected']:
            raise Fault('Package probe failed; active manifest was not changed')
    from .app import read_json
    config = read_json(outpath)
    binding = {'resource': resource, 'contract': p['contract'], 'argv': argv, 'package': package_id}
    config['offers'] = [o for o in config['offers'] if (
        o['resource'], o['contract']) != (resource, p['contract'])] + [binding]
    tmp = outpath + '.' + uid()
    with open(tmp, 'x') as f:
        json.dump(config, f, indent=2)
    os.replace(tmp, outpath)
    return {'package': package_id, 'probes': len(p['probes']), 'manifest': outpath, 'next': 'Restart the provider to advertise the verified offer.'}
