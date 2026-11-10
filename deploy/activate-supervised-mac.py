"""Activate the already-staged Mac runner without interrupting claimed commands."""
import json
import hashlib
import re
import signal
import os
from pathlib import Path
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

HOME=Path.home()
STATE=HOME/'.local/share'
CHECKOUT=STATE/'biomerte-command-runner-20261110'
STAGE=STATE/'biomerte-releases/supervised-recovery-e2be98db'
TERMINAL=STATE/'biome-mac-terminal'
TERMINAL_STAGE=TERMINAL/'runtime-supervised-e2be98db'
CONTROLLER=STATE/'biomerte-controller'


def atomic(path,data):
    fd,name=tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd,'wb') as output:
            output.write(data);output.flush();os.fsync(output.fileno())
        os.replace(name,str(path))
    finally:
        if os.path.exists(name):os.unlink(name)


def domain(label):
    for parent in ('gui/'+str(os.getuid()),'user/'+str(os.getuid()),'system'):
        if subprocess.call(['/bin/launchctl','print',parent+'/'+label],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)==0:
            return parent
    raise RuntimeError('Running service not found: '+label)


def main():
    sys.path.insert(0,str(STAGE))
    from biomerte.worker import Remote
    node=json.loads((CONTROLLER/'node.json').read_text())
    remote=Remote('http://127.0.0.1:'+str(node['port']),node['site'],node['node'],None,token=node['admin_token'])
    groups=[]
    for contract,resource in (('host.terminal.run:1','terminal.mac.ae3c74c06d1909a1ccdc'),
                              ('docker.cli:1','docker.mac.ae3c74c06d1909a1ccdc')):
        offers=remote.call('cap.discover',{'contract':contract})['offers']
        selected=[o for o in offers if o['resource']==resource]
        if len(selected)!=1:raise RuntimeError('Expected one live offer for '+resource)
        groups.append(selected[0])
    plist=HOME/'Library/LaunchAgents/org.biomerte.controller.plist'
    original=plist.read_bytes();config=plistlib.loads(original)
    manifest=json.loads((STAGE/'supervised-stage.json').read_text())
    if os.path.realpath(config['WorkingDirectory'])!=os.path.realpath(manifest['live']):
        raise RuntimeError('Controller source moved since staging; preserve concurrent deployment')
    for root in (STAGE,TERMINAL_STAGE):
        record=json.loads((root/'supervised-stage.json').read_text())
        if record['conflicts']:raise RuntimeError('Unresolved staged merge')
        for entry in record['files']:
            import hashlib
            live=Path(record['live'])/entry['path']
            if entry['live_sha256'] is not None and hashlib.sha256(live.read_bytes()).hexdigest() not in (entry['live_sha256'],entry['staged_sha256']):
                raise RuntimeError('Live source changed since staging: '+str(live))
    controller_domain=domain('org.biomerte.controller')
    terminal_domain=domain('org.biomerte.mac-terminal')
    docker_domain=domain('org.biomerte.mac-docker')
    if '--apply' not in sys.argv:
        print(json.dumps({'ready':True,'stage':str(STAGE),'domains':[controller_domain,terminal_domain,docker_domain]}))
        return
    for offer in groups:
        args={k:offer[k] for k in ('provider','contract','resource','configuration','service','capacity') if k in offer}
        remote.call('cap.offer',dict(args,state='available_after_setup'))
    print('Admissions paused; waiting for existing execution receipts',flush=True)
    while True:
        active=[]
        for name in ('biome-mac-terminal','biome-mac-docker'):
            db=sqlite3.connect(str(STATE/name/'worker.sqlite'))
            try:active.extend([r[0] for r in db.execute("SELECT id FROM actions WHERE state='uncertain'")])
            finally:db.close()
        for offer in groups:
            queue=remote.call('provider.poll',{'provider':offer['provider']})
            active.extend(a['id'] for a in queue.get('actions',[]) if a.get('state')=='claimed')
        if not active:break
        print('Waiting for '+str(len(set(active)))+' claimed executors',flush=True)
        time.sleep(1)
    for entry in json.loads((TERMINAL_STAGE/'supervised-stage.json').read_text())['files']:
        target=TERMINAL/'runtime'/entry['path'];target.parent.mkdir(parents=True,exist_ok=True)
        atomic(target,(TERMINAL_STAGE/entry['path']).read_bytes())
    atomic(TERMINAL/'terminal_adapter.py',(CHECKOUT/'providers/mac-terminal/terminal_adapter.py').read_bytes())
    docker_config=STATE/'biome-mac-docker/stdlib-launcher.json'
    atomic(docker_config,json.dumps({'source':str(STAGE/'providers/mac-docker/mac_docker.py')}).encode())
    # Preserve the existing launchd job, service path and concurrent source overlays.
    live=Path(manifest['live'])
    for entry in sorted(manifest['files'],key=lambda e:e['path'] in
                        ('biomerte/providers.py','biomerte/lifecycle.py','biomerte/worker.py')):
        target=live/entry['path'];data=(STAGE/entry['path']).read_bytes()
        compile(data,str(target),'exec');atomic(target,data)
    def restart(parent,label):
        status=subprocess.check_output(['/bin/launchctl','print',parent+'/'+label]).decode()
        match=re.search(r'\bpid = (\d+)',status)
        if not match:raise RuntimeError('Service PID unavailable: '+label)
        pid=int(match.group(1))
        owner=int(subprocess.check_output(['/bin/ps','-o','uid=','-p',str(pid)]).decode().strip())
        if owner!=os.getuid():raise RuntimeError('Service belongs to another account: '+label)
        os.kill(pid,signal.SIGTERM)
    restart(controller_domain,'org.biomerte.controller')
    for attempt in range(50):
        try:remote.call('cap.recovery.list',{'limit':1});break
        except Exception:time.sleep(.3)
    else:raise RuntimeError('New controller recovery interface unavailable; maintenance remains online')
    restart(terminal_domain,'org.biomerte.mac-terminal')
    restart(docker_domain,'org.biomerte.mac-docker')
    print(json.dumps({'activated':True,'stage':str(STAGE),'controller':'restarted','terminal':'restarted',
                      'docker':'restarted','gateway':'preserved','stream_router':'preserved'}),flush=True)


if __name__=='__main__':
    main()
