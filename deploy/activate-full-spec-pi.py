"""Install an exact integrated controller release while preserving node state.

Run as the Pi service user. Only the service override/restart uses sudo.
Python 3.4 compatible; no original command, worker ledger or firmware is replayed.
"""
import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.request import Request, urlopen

HOME='/home/pi/.local/share'
REPOSITORY='https://api.github.com/repos/michaelrolphone-cmyk/BiomeRTE/tarball/'


def extract(archive, target):
    with tarfile.open(archive, 'r:gz') as source:
        for member in source:
            parts=member.name.split('/')[1:]
            if not parts or parts==['']:
                continue
            if any(part in ('..','') for part in parts) or member.name.startswith('/'):
                raise ValueError('Unsafe source archive path')
            path=os.path.join(target,*parts)
            if member.isdir():
                os.makedirs(path,exist_ok=True)
            elif member.isfile():
                os.makedirs(os.path.dirname(path),exist_ok=True)
                with source.extractfile(member) as incoming, open(path,'wb') as outgoing:
                    shutil.copyfileobj(incoming,outgoing)
                os.chmod(path,member.mode & 0o755)
            else:
                raise ValueError('Unexpected source archive entry')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('commit')
    parser.add_argument('--expected-current',required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    if not re.match(r'^[a-f0-9]{40}$',args.commit):
        raise ValueError('Full source commit required')
    if os.path.expanduser('~')!='/home/pi':
        raise ValueError('Run as pi, without sudo around Python')
    current=subprocess.check_output(['systemctl','show','biomerte.service','-p','WorkingDirectory']).decode().strip().split('=',1)[1]
    target=HOME+'/biomerte-releases/full-spec-'+args.commit[:12]
    if current not in (args.expected_current,target):
        raise ValueError('Controller source changed concurrently: '+current)
    if not os.path.exists(target):
        stage=tempfile.mkdtemp(prefix='full-spec-stage-',dir=HOME+'/biomerte-releases')
        try:
            archive=os.path.join(stage,'source.tar.gz')
            subprocess.check_call(['curl','-fLsS','--retry','2',REPOSITORY+args.commit,'-o',archive])
            extract(archive,stage)
            os.unlink(archive)
            for name in ('app','api','providers','lifecycle','resource_recovery','worker','supervised_commands'):
                path=stage+'/biomerte/'+name+'.py'
                with open(path,'rb') as stream:compile(stream.read(),path,'exec')
            for name in ('session_api.py','retention.py'):
                with open(current+'/biomerte/'+name,'rb') as old, open(stage+'/biomerte/'+name,'rb') as new:
                    if old.read()!=new.read():raise ValueError('Existing session/retention source differs: '+name)
            os.rename(stage,target)
            stage=None
        finally:
            if stage and os.path.exists(stage):shutil.rmtree(stage)
    print('Prepared exact source '+args.commit,flush=True)
    if not args.apply:
        return
    unit='[Service]\nWorkingDirectory='+target+'\nEnvironment=PYTHONPATH='+target+'\n'
    override='/etc/systemd/system/biomerte.service.d/zzzzzzzzzzzzzzzzz-full-spec-integrated.conf'
    process=subprocess.Popen(['sudo','-n','tee',override],stdin=subprocess.PIPE,stdout=subprocess.PIPE)
    process.communicate(unit.encode())
    if process.returncode:raise RuntimeError('Could not install controller service override')
    subprocess.check_call(['sudo','-n','systemctl','daemon-reload'])
    subprocess.check_call(['sudo','-n','systemctl','restart','biomerte.service'])
    subprocess.check_call(['systemctl','is-active','biomerte.service'])
    config=json.load(open(HOME+'/biomerte/node.json'))
    payload={'jsonrpc':'2.0','id':'verify-integrated-recovery','method':'tools/call',
             'params':{'name':'invoke','arguments':{'operation':'cap.recovery.list','arguments':{}}}}
    last=None
    for attempt in range(20):
        try:
            request=Request('http://127.0.0.1:'+str(config['port'])+'/mcp',
                data=json.dumps(payload).encode(),headers={'Content-Type':'application/json',
                'Authorization':'Bearer '+config['admin_token']})
            with urlopen(request,timeout=5) as response:reply=json.loads(response.read().decode())
            if reply.get('result',{}).get('isError'):raise RuntimeError('Recovery API rejected verification')
            if 'result' not in reply:raise RuntimeError('Recovery API unavailable')
            print('Integrated controller active; cap.recovery.list restored. Node identity, signing secret, worker ledgers and firmware preserved.',flush=True)
            return
        except Exception as error:
            last=error
            time.sleep(.25)
    raise RuntimeError('Controller verification failed: '+type(last).__name__)


if __name__=='__main__':main()
