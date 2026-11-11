"""Integrate tested custody code into current runtime overlays without losing stream changes."""
import difflib
import hashlib
import json
import os
import plistlib
import signal
import subprocess
import sys
import tempfile
import time

CORE = ('biomerte/worker.py','biomerte/providers.py','biomerte/lifecycle.py',
        'biomerte/command_runner.py','biomerte/supervised_commands.py',
        'biomerte/resource_recovery.py','biomerte/command_watch.py')


def read(path):
    with open(path,'rb') as source:return source.read()


def atomic(path,data):
    os.makedirs(os.path.dirname(path),exist_ok=True)
    mode=os.stat(path).st_mode & 511 if os.path.exists(path) else 420
    fd,tmp=tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd,'wb') as output:
            output.write(data);output.flush();os.fsync(output.fileno())
        os.chmod(tmp,mode);os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def service_value(name,key):
    value=subprocess.check_output(['systemctl','show',name,'-p',key]).decode().strip()
    return value.split('=',1)[1]


def targets(bundle):
    result={}
    if sys.platform=='darwin':
        home='/Users/micahelbyrns/.local/share'
        with open('/Users/micahelbyrns/Library/LaunchAgents/org.biomerte.controller.plist','rb') as f:
            controller=plistlib.load(f)['WorkingDirectory']
        with open(home+'/biome-mac-docker/stdlib-launcher.json') as f:
            docker=json.load(f)['source']
        roots=[controller,home+'/biome-mac-terminal/runtime',os.path.dirname(os.path.dirname(os.path.dirname(docker)))]
    else:
        controller=service_value('biomerte.service','WorkingDirectory')
        roots=[controller,service_value('biomerte-terminal.service','WorkingDirectory'),
               service_value('biomerte-docker.service','WorkingDirectory')]
    # Native adapters already carry the separately qualified upload fix.
    # Upgrade the controller and worker machinery, preserving those adapters.
    for root in roots:
        if not root or not os.path.isdir(root):raise RuntimeError('Runtime root unavailable: '+str(root))
        for source in CORE:result[os.path.join(root,source)]=source
    return controller,dict((os.path.realpath(k),v) for k,v in result.items())


def stage(bundle,directory):
    os.makedirs(directory,exist_ok=True)
    controller,selected=targets(bundle)
    entries=[]
    for index,(path,source) in enumerate(sorted(selected.items())):
        proposed=bundle['files'][source]['new'].encode()
        old=read(path) if os.path.exists(path) else None
        candidates=[x.encode() for x in bundle['files'][source]['bases']]
        if old is None or old in candidates or old==proposed:
            merged=proposed
        else:
            base=max(candidates,key=lambda x:difflib.SequenceMatcher(None,old,x).ratio()) if candidates else b''
            with tempfile.TemporaryDirectory() as tmp:
                paths=[os.path.join(tmp,str(i)) for i in range(3)]
                for name,data in zip(paths,(old,base,proposed)):atomic(name,data)
                child=subprocess.Popen(['git','merge-file','-p']+paths,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                merged,error=child.communicate()
                if child.returncode:raise RuntimeError('Runtime overlay conflict: '+path)
        compile(merged,path,'exec')
        staged=os.path.join(directory,str(index)+'.py');atomic(staged,merged)
        entries.append(dict(path=path,staged=staged,old=hashlib.sha256(old).hexdigest() if old is not None else None,
                            new=hashlib.sha256(merged).hexdigest()))
    plan=dict(controller=controller,entries=entries,source_tree=bundle['tree'],platform=sys.platform)
    atomic(os.path.join(directory,'plan.json'),json.dumps(plan,sort_keys=True).encode())
    return plan


def mac_restart(label):
    for domain in ('gui/'+str(os.getuid()),'user/'+str(os.getuid()),'system'):
        process=subprocess.Popen(['/bin/launchctl','print',domain+'/'+label],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        output=process.communicate()[0].decode()
        if process.returncode==0:
            import re
            match=re.search(r'\bpid = (\d+)',output)
            if not match:raise RuntimeError('Service has no PID: '+label)
            os.kill(int(match.group(1)),signal.SIGTERM)
            return
    raise RuntimeError('Service unavailable: '+label)


def activate(directory):
    plan=json.loads(read(os.path.join(directory,'plan.json')).decode())
    controller,current=targets({})
    if os.path.realpath(controller)!=os.path.realpath(plan['controller']):
        raise RuntimeError('Controller moved after staging')
    for entry in plan['entries']:
        path=entry['path'];old=hashlib.sha256(read(path)).hexdigest() if os.path.exists(path) else None
        if old not in (entry['old'],entry['new']):raise RuntimeError('Source changed after staging: '+path)
        if hashlib.sha256(read(entry['staged'])).hexdigest()!=entry['new']:raise RuntimeError('Staged bytes changed')
    # Every source is checked and compiled before any live source changes.
    for entry in plan['entries']:atomic(entry['path'],read(entry['staged']))
    sys.path.insert(0,controller)
    from biomerte.worker import Remote
    state=('/Users/micahelbyrns/.local/share/biomerte-controller' if sys.platform=='darwin'
           else '/home/pi/.local/share/biomerte')
    cfg=json.loads(read(state+'/node.json').decode())
    remote=Remote('http://127.0.0.1:'+str(cfg['port']),cfg['site'],cfg['node'],None,token=cfg['admin_token'])
    if sys.platform=='darwin':
        mac_restart('org.biomerte.controller')
    else:
        for name in ('biomerte-terminal','biomerte-docker'):
            drop='/etc/systemd/system/'+name+'.service.d'
            subprocess.check_call(['sudo','-n','mkdir','-p',drop])
            with tempfile.NamedTemporaryFile(delete=False) as f:
                f.write(b'[Service]\\nKillMode=process\\n'.replace(b'\\n',b'\n'));tmp=f.name
            try:subprocess.check_call(['sudo','-n','install','-m','0644',tmp,drop+'/99-integrated-custody.conf'])
            finally:os.unlink(tmp)
        subprocess.check_call(['sudo','-n','systemctl','daemon-reload'])
        subprocess.check_call(['sudo','-n','systemctl','restart','biomerte.service'])
    while True:
        try:
            remote.call('cap.recovery.list',{'limit':1})
            break
        except Exception as error:
            print('Awaiting controller recovery interface: '+type(error).__name__,flush=True)
            time.sleep(1)
    if sys.platform=='darwin':
        mac_restart('org.biomerte.mac-terminal')
        mac_restart('org.biomerte.mac-docker')
    else:
        subprocess.check_call(['sudo','-n','systemctl','restart','biomerte-terminal.service','biomerte-docker.service'])
    print('INTEGRATED_CUSTODY_ACTIVE '+plan['source_tree'],flush=True)
    return plan
