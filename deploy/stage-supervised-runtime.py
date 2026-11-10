"""Stage runner changes by merging them into the effective live source (3.4+)."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

BASE='d22caeb26f327d9521c8a5912abd9d705012cb01'
FILES=('biomerte/worker.py','biomerte/providers.py','biomerte/lifecycle.py',
       'biomerte/command_runner.py','biomerte/supervised_commands.py',
       'biomerte/resource_recovery.py','biomerte/command_watch.py')


def stage(checkout,live,destination):
    checkout,live,destination=map(os.path.abspath,(checkout,live,destination))
    if os.path.exists(destination):
        raise RuntimeError('Stage destination already exists; inspect its original result')
    os.makedirs(destination,mode=0o700)
    for name in ('biomerte','deploy','examples'):
        source=os.path.join(live,name)
        if os.path.isdir(source):
            shutil.copytree(source,os.path.join(destination,name),
                            ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    records=[]
    conflicts=[]
    for relative in FILES:
        current=os.path.join(live,relative)
        target=os.path.join(destination,relative)
        proposed=os.path.join(checkout,relative)
        with open(proposed,'rb') as stream:new=stream.read()
        try:
            base=subprocess.check_output(['git','show',BASE+':'+relative],cwd=checkout,stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            base=None
        old=open(current,'rb').read() if os.path.exists(current) else None
        if old is None or base is None or old==base:
            merged=new
        elif new==base:
            merged=old
        else:
            with tempfile.TemporaryDirectory() as folder:
                paths=[os.path.join(folder,str(i)) for i in range(3)]
                for path,data in zip(paths,(old,base,new)):
                    with open(path,'wb') as stream:stream.write(data)
                process=subprocess.Popen(['git','merge-file','-p']+paths,stdout=subprocess.PIPE)
                merged=process.communicate()[0]
                if process.returncode:
                    conflicts.append(relative)
        with open(target,'wb') as stream:stream.write(merged)
        records.append(dict(path=relative,live_sha256=hashlib.sha256(old).hexdigest() if old else None,
                            staged_sha256=hashlib.sha256(merged).hexdigest()))
    manifest=dict(live=live,checkout=checkout,
                  revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=checkout).decode().strip(),
                  files=records,conflicts=conflicts)
    with open(os.path.join(destination,'supervised-stage.json'),'w') as stream:json.dump(manifest,stream,indent=2)
    if conflicts:
        raise RuntimeError('Merge conflicts in staged source: '+', '.join(conflicts))
    for record in records:
        path=os.path.join(destination,record['path'])
        with open(path,'rb') as stream:compile(stream.read(),path,'exec')
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--checkout',required=True)
    parser.add_argument('--live',required=True)
    parser.add_argument('--destination',required=True)
    args=parser.parse_args()
    print(json.dumps(stage(args.checkout,args.live,args.destination),sort_keys=True))
