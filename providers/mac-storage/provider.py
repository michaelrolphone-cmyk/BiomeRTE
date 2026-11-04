"""External Mac file storage using BiomeRTE's shared worker and Pi file operations."""
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import quote
from volumes import BASE,MOUNT,IMAGE,VAULT,WORKSPACE,SACRED,prepare,mounted
from backup import HEALTH,entries

HERE=Path(__file__).resolve().parent
TERMINAL=Path.home()/'.local/share/biome-mac-terminal'
sys.path[:0]=[str(HERE.parent/'pi-storage'),str(TERMINAL),str(TERMINAL/'runtime')]
import storage
from usage import file_usage
import mac_provider as gateway
NAMESPACE='Shared project namespace: 1–63 lowercase ASCII letters, digits or hyphens, beginning with a letter. Create it with storage.namespace.create:1 first.'
DURABILITY='98 GiB Mac workspace, unreplicated and unavailable when the Mac is offline. Paths are relative to the required namespace. '
SACRED_GUIDE='Read-only Mac backup of the authoritative 2 GiB Pi volume. Pulls every five minutes while both hosts are available. Local restore works without the Pi controller.'
EXTRA={
    'create':('storage.namespace.create',{'namespace':'str'},'Create a workspace namespace. Existing namespaces are returned unchanged.'),
    'remove':('storage.namespace.remove',{'namespace':'str'},'Remove an empty namespace with no outstanding handles.'),
    'capacity':('storage.workspace.capacity',{},'Read reserved workspace bytes, usable filesystem bytes, free bytes and durability.'),
    'replica':('storage.sacred.status',{},'Read last successful backup, source identity, freshness, retained generations and recovery instructions.')}
def good(value):return {'ok':True,'result':value}
def bad(code):return {'ok':False,'error':code}

def journal_headroom_low():
    fs=os.statvfs(MOUNT)
    return fs.f_bavail*fs.f_frsize < 16*1024**2

def operation(op,a):
    try:return file_operation(op,a)
    except OSError as exc:
        if exc.errno in (errno.ENOSPC,errno.EDQUOT):return bad('NO_SPACE')
        raise
    except sqlite3.OperationalError as exc:
        if 'database or disk is full' in str(exc):return bad('NO_SPACE')
        raise

def file_operation(op,a):
    if op=='replica':
        if not VAULT.is_file():return bad('VAULT_OFFLINE')
        h=json.loads(HEALTH.read_text()) if HEALTH.is_file() else {'state':'awaiting_first_copy'}
        if h.get('state')=='current' and time.time()-h.get('last_success',0)>660:h['state']='stale'
        with tarfile.open(VAULT,'r:') as ar:history=entries(ar)
        return good(dict(h,reserved_bytes=SACRED,writable=False,retained=[{k:g[k] for k in
                         ('id','time','sha256','size','volume')} for g in history],
                         recovery=str(BASE/'current/providers/mac-storage/backup.py')+' restore NEW_DATABASE [--generation ID]'))
    if not mounted():return bad('WORKSPACE_OFFLINE')
    if op=='capacity':
        fs=os.statvfs(MOUNT)
        return good(dict(reserved_bytes=WORKSPACE,usable_bytes=fs.f_blocks*fs.f_frsize,free_bytes=fs.f_bavail*fs.f_frsize,
                         tier='volatile_workspace',replicated=False,namespace_required=True))
    ns=a.get('namespace')
    if not isinstance(ns,str) or not re.fullmatch('[a-z][a-z0-9-]{0,62}',ns):return bad('INVALID_NAMESPACE')
    root=MOUNT/'namespaces'
    root.mkdir(mode=0o700,exist_ok=True)
    path=root/(ns+'.sqlite')
    with (BASE/'namespaces.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX if op in ('create','remove') else fcntl.LOCK_SH)
        if path.is_symlink():return bad('INVALID_NAMESPACE')
        if op=='create':
            if not path.exists():
                with tempfile.TemporaryDirectory(dir=root) as td:
                    tmp=Path(td)/'new'
                    d=sqlite3.connect(tmp,isolation_level=None)
                    try:
                        d.executescript(storage.SCHEMA)
                        d.execute('INSERT INTO volume VALUES(?)',(ns,))
                    finally:d.close()
                    os.replace(tmp,path)
            return good(dict(namespace=ns))
        if not path.exists():return bad('NAMESPACE_NOT_FOUND')
        db=sqlite3.connect('file:'+quote(str(path))+'?mode=rw',uri=True,isolation_level=None,timeout=30)
        try:
            if db.execute('SELECT uuid FROM volume').fetchall()!=[(ns,)]:return bad('WRONG_NAMESPACE')
            if op=='remove':
                if db.execute('SELECT 1 FROM nodes WHERE id!=1 LIMIT 1').fetchone() or db.execute('SELECT 1 FROM handles LIMIT 1').fetchone():
                    return bad('NAMESPACE_NOT_EMPTY')
            else:
                if (op in ('write','mkdir') or (op=='open' and a.get('mode','')[:1] in ('a','x'))) and journal_headroom_low():
                    return bad('NO_SPACE')
                return storage.dispatch(db,op,a)
        finally:db.close()
        path.unlink()
        return good(dict(namespace=ns))

def definitions():
    for op,(fields,description) in storage.OPERATIONS.items():
        guide=file_usage(op)
        guide['before']=DURABILITY+guide['before'].replace('Release this contract lease before acquiring another file contract on the same resource. ','')
        guide['parameters']=dict(guide['parameters'],namespace=NAMESPACE)
        for k,v in guide['parameters'].items():guide['parameters'][k]=v.replace('storage.file.open:1','storage.file.open:2').replace('inside the shared volume','inside the selected namespace')
        guide['example_inputs']=dict(guide['example_inputs'],namespace='project-demo')
        yield op,'storage.file.'+op,2,dict(fields,namespace='str'),description,guide
    for op,(name,fields,description) in EXTRA.items():
        guide={'before':SACRED_GUIDE if op=='replica' else DURABILITY,'parameters':{'namespace':NAMESPACE} if fields else {},
               'example_inputs':{'namespace':'project-demo'} if fields else {},'read_result':'Check result.ok, then read result.result. '+description}
        yield op,name,1,fields,description,guide

class Remote:
    realm='site'
    def __init__(self):
        terminal=json.loads((TERMINAL/'identity/node.json').read_text())['node']
        self.site=json.loads((TERMINAL/'terminal-manifest.json').read_text())['site']
        self.node=hashlib.sha256(('biomerte/mac/storage/v1/'+terminal).encode()).hexdigest()
        self.suffix=terminal[:20]
    def call(self,name,args,rid=None):return gateway.invoke(name,args,rid,self.realm)

def serve():
    from biomerte.worker import Worker,ActionPool
    remote=Remote()
    prepare()
    offers=[]
    for op,name,version,fields,description,guide in definitions():
        remote.call('cap.define',dict(name=name,version=version,inputs=fields,outputs=storage.OUTPUTS,description=description,usage=guide))
        replica=op=='replica'
        config=dict(tier='sacred_replica' if replica else 'volatile_workspace',reserved_bytes=SACRED if replica else WORKSPACE,
                    namespace_required=not replica,replicated=replica,read_only=replica,host=socket.gethostname())
        offers.append(dict(contract=name+':'+str(version),resource=('sacred.mac.' if replica else 'workspace.mac.')+remote.suffix,
                           service='sacred-replica' if replica else 'workspace-storage',capacity=4 if replica else 16,usage=guide,
                           configuration=config,argv=[sys.executable,str(HERE/'provider.py'),'execute',op]))
    worker=Worker(str(BASE/'worker.sqlite'),remote,offers,120);pool=ActionPool(worker,20)
    stop=False
    def stopped(*unused):
        nonlocal stop
        stop=True
    signal.signal(signal.SIGTERM,stopped)
    states={}
    try:
        while not stop:
            try:
                for o in offers:
                    state='ready' if (VAULT.exists() if o['service']=='sacred-replica' else mounted()) else 'offline'
                    if states.get(o['contract'])!=state:
                        remote.call('cap.offer',dict({k:v for k,v in o.items() if k!='argv'},provider=remote.node,state=state))
                        states[o['contract']]=state
                count=worker.once(pool)
                time.sleep(.3 if count else 2)
            except Exception as e:
                print('Worker retry:',str(e)[:300],file=sys.stderr,flush=True);time.sleep(4)
    finally:
        pool.executor.shutdown(wait=True)
        for o in offers:remote.call('cap.offer',dict({k:v for k,v in o.items() if k!='argv'},provider=remote.node,state='offline'))
        worker.db.close()

def install():
    # Use the login domain also used by the Mac Docker provider.
    target='gui/'+str(os.getuid())
    for suffix,argv,policy in [('replica',[str(HERE/'backup.py'),'sync'],dict(StartInterval=300)),
                              ('storage',[str(HERE/'provider.py'),'serve'],dict(KeepAlive=True,ThrottleInterval=20))]:
        label='org.biomerte.mac-'+suffix
        job=target+'/'+label
        path=Path.home()/'Library/LaunchAgents'/(label+'.plist');path.parent.mkdir(parents=True,exist_ok=True)
        spec=dict(policy,Label=label,ProgramArguments=[sys.executable]+argv,WorkingDirectory=str(BASE),
                  RunAtLoad=True,Umask=0o077,StandardOutPath=str(BASE/(suffix+'.log')),StandardErrorPath=str(BASE/(suffix+'-error.log')),
                  EnvironmentVariables=dict(HOME=str(Path.home()),PATH='/usr/bin:/bin:/usr/sbin:/sbin',PYTHONUNBUFFERED='1'))
        if subprocess.run(['/bin/launchctl','print',job],capture_output=True).returncode==0:
            subprocess.run(['/bin/launchctl','bootout',job],check=True)
        tmp=path.with_suffix('.tmp');tmp.write_bytes(plistlib.dumps(spec));os.replace(tmp,path)
        subprocess.run(['/bin/launchctl','enable',job],check=True)
        for i in range(31):
            p=subprocess.run(['/bin/launchctl','bootstrap',target,str(path)],capture_output=True)
            if not p.returncode:break
            if p.returncode!=5 or i==30:raise RuntimeError(p.stderr.decode())
            time.sleep(1)
        print(label,flush=True)

if __name__=='__main__':
    os.umask(0o077)
    if sys.argv[1]=='execute':print(json.dumps(operation(sys.argv[2],json.load(sys.stdin)['inputs'])))
    elif sys.argv[1]=='serve':serve()
    elif sys.argv[1]=='install':install()
