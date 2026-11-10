"""Activate the staged Pi runner through existing services; Python 3.4+."""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

HOME='/home/pi/.local/share'
CHECKOUT=HOME+'/biomerte-runner-source-20261110'
STAGE=HOME+'/biomerte-releases/supervised-recovery-e2be98db'
CONTROLLER=HOME+'/biomerte'


def atomic(path,data):
    fd,name=tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd,'wb') as out:
            out.write(data);out.flush();os.fsync(out.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def main():
    manifest=json.load(open(STAGE+'/supervised-stage.json'))
    live=manifest['live']
    for entry in manifest['files']:
        path=live+'/'+entry['path']
        current=hashlib.sha256(open(path,'rb').read()).hexdigest() if os.path.exists(path) else None
        if current not in (entry['live_sha256'],entry['staged_sha256']):
            raise RuntimeError('Concurrent source change: '+path)
    sys.path.insert(0,live)
    from biomerte.worker import Remote
    cfg=json.load(open(CONTROLLER+'/node.json'))
    admin=Remote('http://127.0.0.1:'+str(cfg['port']),cfg['site'],cfg['node'],None,token=cfg['admin_token'])
    providers=[]
    for name in ('biomerte-terminal','biomerte-docker'):
        directory=HOME+'/'+name
        node=json.load(open(directory+'/node.json'))
        provider_manifest=json.load(open(directory+'/manifest.json'))
        offer=provider_manifest['offers'][0]
        providers.append((directory,node,provider_manifest,offer))
    if '--apply' not in sys.argv:
        print(json.dumps({'ready':True,'live':live,'stage':STAGE,'providers':[x[1]['node'] for x in providers]}))
        return
    for directory,node,provider_manifest,offer in providers:
        scopes=['cap.offer','cap.fenced','provider.poll','provider.claim','provider.result','provider.custody',
                'provider.reconcile','cap.heartbeat','cap.command.state','agent.progress','cap.status',
                'artifact.get','artifact.put','offer:'+offer['contract'],'resource:'+offer['resource']]
        admin.call('peer.trust',{'public_key':node['public_key'],'label':'Pi supervised '+offer['contract'],'scopes':scopes})
        options={k:offer[k] for k in ('contract','resource','service','capacity','configuration') if k in offer}
        options.update(provider=node['node'],state='available_after_setup')
        admin.call('cap.offer',options)
    # The installed terminal supervisor persists effects before transmission.
    # Wait for live jobs; finished historical receipts do not block an upgrade.
    journal=HOME+'/biomerte-terminal/terminal.sqlite'
    while True:
        db=sqlite3.connect(journal)
        try:active=db.execute("SELECT id,pid FROM jobs WHERE state IN ('launching','running')").fetchall()
        finally:db.close()
        active=[r for r in active if r[1] and os.path.exists('/proc/'+str(r[1]))]
        if not active:break
        print('Waiting for native terminal custodians: '+str([x[0] for x in active]),flush=True)
        time.sleep(1)
    for entry in manifest['files']:
        path=live+'/'+entry['path']
        data=open(STAGE+'/'+entry['path'],'rb').read()
        compile(data,path,'exec')
        atomic(path,data)
    for relative in ('providers/pi-docker/docker_cli.py','providers/pi-terminal/terminal.py'):
        target=live+'/'+relative
        os.makedirs(os.path.dirname(target),exist_ok=True)
        shutil.copyfile(CHECKOUT+'/'+relative,target)
    directory,node,provider_manifest,offer=providers[1]
    offer['argv']=[sys.executable,'-S',live+'/providers/pi-docker/docker_cli.py',directory+'/manifest.json']
    atomic(directory+'/manifest.json',json.dumps(provider_manifest,indent=2).encode())
    for name,directory in (('biomerte-terminal',HOME+'/biomerte-terminal'),('biomerte-docker',HOME+'/biomerte-docker')):
        unit='[Service]\nKillMode=process\nWorkingDirectory='+live+'\nEnvironment=PYTHONPATH='+live+'\nExecStart=\nExecStart=/usr/bin/python3 -S -m biomerte --home '+directory+' worker '+directory+'/manifest.json\n'
        with tempfile.NamedTemporaryFile(delete=False) as stream:
            stream.write(unit.encode());temporary=stream.name
        target='/etc/systemd/system/'+name+'.service.d'
        try:
            subprocess.check_call(['sudo','-n','mkdir','-p',target])
            subprocess.check_call(['sudo','-n','install','-m','0644',temporary,target+'/99-supervised-runner.conf'])
        finally:os.unlink(temporary)
    subprocess.check_call(['sudo','-n','systemctl','daemon-reload'])
    subprocess.check_call(['sudo','-n','systemctl','restart','biomerte.service'])
    for attempt in range(50):
        try:admin.call('cap.recovery.list',{'limit':1});break
        except Exception:time.sleep(.3)
    else:raise RuntimeError('Controller recovery interface did not start')
    subprocess.check_call(['sudo','-n','systemctl','restart','biomerte-terminal.service','biomerte-docker.service'])
    print(json.dumps({'activated':True,'live':live,'gateway':'preserved','providers':'restarted'}),flush=True)


if __name__=='__main__':
    main()
