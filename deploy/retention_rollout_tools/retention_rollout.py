#!/usr/bin/env python3
"""Local preparation + explicit native qualification/apply; never enrolls or purges."""
import argparse, hashlib, json, os, pathlib, platform, shutil, socket, stat
import subprocess, sys, tempfile, time, sqlite3
P = pathlib.Path
# Keep the native runner compatible with the Pi's older Python 3 stdlib.
def read_path_bytes(self):
    with self.open('rb') as handle:return handle.read()
def read_path_text(self):
    with self.open('r') as handle:return handle.read()
if not hasattr(P, 'read_bytes'):P.read_bytes=read_path_bytes
if not hasattr(P, 'read_text'):P.read_text=read_path_text
def below(path, root):
    return path == root or root in path.parents


def digest(data): return hashlib.sha256(data).hexdigest()
def read_json(p): return json.loads(P(p).read_text())
def write(p, data, mode=0o600):
    p=P(p); os.makedirs(str(p.parent), exist_ok=True)
    fd,t=tempfile.mkstemp(prefix='.'+p.name+'.',dir=str(p.parent))
    try:
        with os.fdopen(fd,'wb') as f: f.write(data); f.flush(); os.fsync(f.fileno())
        os.chmod(t,mode); os.replace(t,str(p))
        fd=os.open(str(p.parent),os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if os.path.exists(t): os.unlink(t)
def dump(p,v): write(p,json.dumps(v,indent=2,sort_keys=True).encode())
def require(ok,msg):
    if not ok: raise RuntimeError(msg)
def cmd(argv):
    require(isinstance(argv,list) and argv and all(isinstance(x,str) for x in argv),'invalid argv')
    return subprocess.check_output(argv,stderr=subprocess.STDOUT,timeout=90).decode()
def probe(argv): return json.loads(cmd(argv))
def confined(root,rel):
    p=P(rel); require(not p.is_absolute() and '..' not in p.parts,'unsafe relative path')
    out=root/p; path_ok(out.absolute()); return out

def prepare(a):
    spec=read_json(a.spec); source=P(a.source).resolve(); bundle=P(a.bundle).resolve()
    require(not spec.get('pending_requirements'),'deployment plan has unresolved requirements')
    require(not bundle.exists(),'bundle exists; use a fresh directory')
    require(not below(bundle,source),'bundle cannot be inside source')
    bundle.mkdir(mode=0o700)
    shutil.copytree(str(source),str(bundle/'fixture'),ignore=shutil.ignore_patterns('.git','__pycache__','*.pyc'))
    spec['files']=[]
    for i,item in enumerate(read_json(a.spec)['files']):
        item=dict(item); data=confined(source,item.pop('source')).read_bytes()
        require(P(item['target']).is_absolute(),'target must be absolute')
        item['payload']='payload/'+str(i); item['new_sha256']=digest(data)
        write(bundle/item['payload'],data); spec['files'].append(item)
    for item in spec.get('config_edits',[]):
        require(item['phase']=='gatewaylink' and item['path']==['gateway_journal','checkpoint_mode'] and item['new']=='bootstrap',
                'only gatewaylink checkpoint_mode=bootstrap config edit allowed')
        require(item['target'] in spec['unchanged_files'] and item['old_sha256']==spec['unchanged_files'][item['target']],
                'config edit must match protected node preimage')
    for item in spec.get('manifest_edits',[]):
        require(item['path'][-1]=='acknowledge_argv','only acknowledge_argv manifest fields allowed')
        require(isinstance(item['new'],list),'acknowledge_argv must be list')
    spec['fixture_hashes']={str(p.relative_to(bundle/'fixture')):digest(p.read_bytes()) for p in sorted((bundle/'fixture').rglob('*')) if p.is_file()}
    shutil.copy2(__file__,str(bundle/'retention_rollout.py'))
    spec['runner_sha256']=digest((bundle/'retention_rollout.py').read_bytes())
    spec['helper_hashes']={}
    for name in spec.pop('helpers',[]):
        require('/' not in name and name.endswith('.py'),'invalid helper name')
        data=P(__file__).with_name(name).read_bytes();write(bundle/name,data)
        spec['helper_hashes'][name]=digest(data)
    def expand(v):
        if isinstance(v,str):return v.replace('@BUNDLE@',spec.get('native_bundle',str(bundle)))
        if isinstance(v,list):return [expand(x) for x in v]
        if isinstance(v,dict):return {k:expand(x) for k,x in v.items()}
        return v
    spec=expand(spec)
    dump(bundle/'plan.json',spec)
    print(json.dumps({'bundle':str(bundle),'plan_sha256':digest((bundle/'plan.json').read_bytes())}))

def verify_bundle_paths(spec,bundle):
    expected=str(P(bundle).resolve())
    require(spec.get('native_bundle')==expected,'native bundle path differs')
    helpers=set(spec.get('helper_hashes',{})) | {'retention_rollout.py'}
    def walk(value):
        if isinstance(value,str):
            if P(value).is_absolute() and P(value).name in helpers:
                require(str(P(value).parent)==expected,'helper path outside frozen bundle')
        elif isinstance(value,list):
            for i,item in enumerate(value):
                if item=='--bundle':
                    require(i+1<len(value) and value[i+1]==expected,'probe bundle argument differs')
                walk(item)
        elif isinstance(value,dict):
            for item in value.values():walk(item)
    walk(spec)

def verified(a):
    b=P(a.bundle).resolve(); plan=b/'plan.json'
    require(digest(plan.read_bytes())==a.plan_sha256,'plan hash differs')
    s=read_json(plan)
    verify_bundle_paths(s,b)
    require(platform.system()==s['platform'] and socket.gethostname()==s['hostname'],'wrong native host')
    require(digest(P(__file__).read_bytes())==s['runner_sha256'],'runner differs')
    for name,h in s.get('helper_hashes',{}).items(): require(digest(confined(b,name).read_bytes())==h,'helper differs')
    for f in s['files']: require(digest(confined(b,f['payload']).read_bytes())==f['new_sha256'],'payload differs')
    return b,s

def qualify(a):
    b,s=verified(a); fixture=b/'fixture'
    if s.get('native_runtime'):
        require(sys.version==s['native_runtime']['python'] and sqlite3.sqlite_version==s['native_runtime']['sqlite'],'native runtime changed')
    actual={str(p.relative_to(fixture)):digest(p.read_bytes()) for p in fixture.rglob('*') if p.is_file()}
    require(actual==s['fixture_hashes'],'fixture differs')
    require(s['tests'] and all(x.startswith('tests.test_') for x in s['tests']),'explicit fixture tests required')
    with tempfile.TemporaryDirectory(prefix='retention-native-fixture-') as t:
        work=P(t)/'source'; shutil.copytree(str(fixture),str(work))
        home=P(t)/'home'; home.mkdir()
        env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'HOME':str(home),'TMPDIR':t,'PYTHONPATH':str(work),'PYTHONDONTWRITEBYTECODE':'1','LANG':'C.UTF-8'}
        p=subprocess.Popen([sys.executable,'-B','-S','-m','unittest','-v']+s['tests'],cwd=str(work),env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        try: output=p.communicate(timeout=s.get('test_timeout',900))[0]
        except subprocess.TimeoutExpired:
            p.kill(); output=p.communicate()[0]; write(b/'qualification.log',output); raise
        write(b/'qualification.log',output)
    import re
    match=re.search(rb'Ran (\d+) tests? in ',output)
    count=int(match.group(1)) if match else 0
    passed=p.returncode==0 and count>=s['minimum_tests'] and b'\nOK' in output and b'skipped=' not in output
    receipt={'passed':passed,'tests':count,'plan_sha256':a.plan_sha256,'platform':platform.system(),'hostname':socket.gethostname(),'python':sys.executable,'python_version':sys.version,'sqlite_version':sqlite3.sqlite_version,'time':time.time(),'log_sha256':digest(output)}
    dump(b/'qualification.json',receipt); print(json.dumps(receipt)); require(passed,'native fixture qualification failed')

def state(s): return {name:cmd(argv).strip() for name,argv in s['preserve_probes'].items()}
def ready(s):
    try: return probe(s['health_probe']).get('ready') is True
    except Exception: return False

def path_ok(p):
    require(p.is_absolute(),'absolute path required')
    for parent in [p]+list(p.parents): require(not parent.is_symlink(),'symlink path denied: '+str(parent))

def manifest_data(original,item):
    data=json.loads(original.decode('utf-8') if isinstance(original,bytes) else original); cursor=data
    for key in item['path'][:-1]: cursor=cursor[key]
    key=item['path'][-1]
    require(cursor.get(key)==item['old'] and (key in cursor)==item['old_present'],'manifest field changed')
    cursor[key]=item['new']
    return (json.dumps(data,indent=2)+'\n').encode()

def completed_baselines(b,s,plan_sha256):
    protected=dict(s['unchanged_files']); states=dict(s['preserve_expected'])
    for name in ('controller','gatewaylink','terminal'):
        p=b/('receipt-'+name+'.json')
        if not p.exists():continue
        receipt=read_json(p)
        if receipt.get('state')!='applied':continue
        require(receipt['plan_sha256']==plan_sha256,'prior receipt belongs to different plan')
        protected.update(receipt.get('protected_hashes_after',{}))
        if name=='gatewaylink' and 'gateway' in states:
            states['gateway']=receipt['preserve_after']['gateway']
    return protected,states

def preserved_equal(before,after,phase):
    return {k:v for k,v in before.items() if not (phase=='gatewaylink' and k=='gateway')} == {
        k:v for k,v in after.items() if not (phase=='gatewaylink' and k=='gateway')}

def terminal_gate_ready(cfg,gate,system):
    mode=cfg.get('restart_safety_mode')
    if mode is None:local=gate.get('local_quiescent') is True
    else:
        require(mode=='process-boundary' and system=='Darwin','unsupported terminal restart safety mode')
        local=gate.get('restart_safety_mode')==mode and gate.get('local_restart_safe') is True
    return gate.get('admission_closed') is True and gate.get('active_commands')==0 and local

def apply(a,preflight_only=False):
    b,s=verified(a); q=read_json(b/'qualification.json')
    require(q['passed'] and q['plan_sha256']==a.plan_sha256,'missing native qualification')
    require(q['hostname']==socket.gethostname() and q['platform']==platform.system(),'wrong qualification host')
    require(digest((b/'qualification.log').read_bytes())==q['log_sha256'],'qualification log differs')
    phase=a.phase; cfg=s['phases'][phase]
    require(not preflight_only or phase=='terminal','preflight-only is terminal scoped')
    tag=phase+'-qualification' if preflight_only else phase
    # No retries after a partially completed run: inspect receipt before preparing a new plan.
    lock=b/('apply-'+tag+'.lock'); fd=os.open(str(lock),os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600); os.close(fd)
    receipt={'phase':phase,'plan_sha256':a.plan_sha256,'state':'preflight','time':time.time()}
    output=b/('receipt-'+tag+'.json'); changed=[]; stopped=False; before=None
    protected_before,expected_states=completed_baselines(b,s,a.plan_sha256)
    protected_after=dict(protected_before)
    def record(): dump(output,receipt)
    record()
    try:
        require(cfg['restart_argv'] and cfg['health_probe'],'restart/health required')
        before=state(s)
        if cfg.get('preflight_argv'): cmd(cfg['preflight_argv'])
        require(before==expected_states,'protected service state differs from reviewed baseline')
        for p,h in protected_before.items(): require(digest(P(p).read_bytes())==h,'protected config changed: '+p)
        if phase=='terminal':
            # The probe must check a persistent admission gate, not merely an empty process list.
            gate=probe(cfg['quiescence_probe'])
            dump(b/('gate-first-'+tag+'.json'),gate)
            require(terminal_gate_ready(cfg,gate,platform.system()),'terminal admission not closed and drained')
            require(os.getppid()!=int(gate.get('worker_pid',0)),'must run outside worker process')
            if platform.system()=='Linux':
                cg=P('/proc/self/cgroup').read_text()
                require('biomerte-terminal.service' not in cg,'executing inside terminal worker cgroup')
            else:
                info=cmd(['/bin/launchctl','print',cfg['standalone_job']])
                require(('pid = '+str(os.getpid())) in info,'not executing as dedicated launchd job')
        entries=[]
        for f in s['files']:
            if f['phase']==phase: entries.append((f,P(f['target']),confined(b,f['payload']).read_bytes()))
        for f in s.get('manifest_edits',[])+s.get('config_edits',[]):
            if f['phase']==phase:
                p=P(f['target']); data=manifest_data(p.read_bytes(),f); entries.append((f,p,data))
                if f in s.get('config_edits',[]):protected_after[str(p)]=digest(data)
        require(entries,'empty phase')
        require(len({str(p) for _,p,_ in entries})==len(entries),'duplicate target; combine edits')
        for i,(f,p,data) in enumerate(entries):
            path_ok(p); exists=p.exists()
            require((digest(p.read_bytes()) if exists else None)==f['old_sha256'],'live preimage differs: '+str(p))
            if exists: require(p.is_file(),'not a regular file')
            if p.suffix=='.py': compile(data,str(p),'exec')
            backup=b/'backups'/tag/str(i)
            require(not backup.exists(),'backup already exists')
            old=p.read_bytes() if exists else None
            mode=stat.S_IMODE(p.stat().st_mode) if exists else f.get('mode',0o644)
            owner=(p.stat().st_uid,p.stat().st_gid) if exists else None
            if exists: write(backup,old,0o600)
            entries[i]=(f,p,data,backup,old,mode,owner)
        # Stop worker only from its independent job after the mandatory gate proof.
        if phase=='terminal':
            final_gate=probe(cfg['quiescence_probe'])
            dump(b/('gate-final-'+tag+'.json'),final_gate)
            require(final_gate==gate,'quiescence changed during preflight')
            if preflight_only:
                receipt.update(state='qualified',no_production_changes=True,stable_gate=gate)
                record();print(json.dumps(receipt));return
            if cfg.get('stop_argv'):
                stopped=True; cmd(cfg['stop_argv'])
                require(probe(cfg['stopped_probe']).get('stopped') is True,'worker did not stop')
        receipt['state']='applying'; record()
        for f,p,data,backup,old,mode,owner in entries:
            require((digest(p.read_bytes()) if p.exists() else None)==f['old_sha256'],'preimage changed during apply')
            changed.append((f,p,data,backup,old,mode,owner))
            write(p,data,mode)
            if owner is not None and (p.stat().st_uid,p.stat().st_gid)!=owner: os.chown(str(p),*owner)
            require(digest(p.read_bytes())==digest(data),'replacement hash failed')
        cmd(cfg['restart_argv'])
        deadline=time.monotonic()+cfg.get('health_timeout',120)
        while not ready(cfg) and time.monotonic()<deadline: time.sleep(1)
        require(ready(cfg),'health failed')
        after=state(s);require(preserved_equal(before,after,phase),'protected services changed')
        for p,h in protected_after.items(): require(digest(P(p).read_bytes())==h,'protected config changed')
        receipt.update(state='applied',health_verified=True,protected_state_unchanged=True,protected_hashes_after=protected_after,preserve_after=after,files={str(p):digest(p.read_bytes()) for _,p,*_ in entries})
        record(); print(json.dumps(receipt))
    except BaseException as e:
        receipt.update(state='failed',error=str(e)); errors=[]
        rollback_safe=True
        if phase=='terminal' and (changed or stopped):
            try:
                if cfg.get('rollback_gate_argv'): cmd(cfg['rollback_gate_argv'])
                current_gate=probe(cfg['quiescence_probe'])
                require(terminal_gate_ready(cfg,current_gate,platform.system()),
                        'terminal rollback requires fresh admission-closed/drained proof')
                if cfg.get('stop_argv'):
                    cmd(cfg['stop_argv'])
                    require(probe(cfg['stopped_probe']).get('stopped') is True,'terminal not stopped for rollback')
            except Exception as er:
                rollback_safe=False;errors.append(str(er))
        for f,p,data,backup,old,mode,owner in reversed(changed if rollback_safe else []):
            try:
                require(p.exists() and digest(p.read_bytes())==digest(data),'refusing rollback over concurrent edit')
                if old is None: p.unlink()
                else:
                    require(digest(backup.read_bytes())==f['old_sha256'],'backup differs')
                    write(p,backup.read_bytes(),mode)
                    if owner is not None and (p.stat().st_uid,p.stat().st_gid)!=owner: os.chown(str(p),*owner)
                require((digest(p.read_bytes()) if p.exists() else None)==f['old_sha256'],'rollback hash failed')
            except Exception as er: errors.append(str(er))
        if rollback_safe and (changed or stopped):
            try:
                cmd(cfg['restart_argv']); deadline=time.monotonic()+cfg.get('health_timeout',120)
                while not ready(cfg) and time.monotonic()<deadline: time.sleep(1)
                require(ready(cfg),'rollback health failed')
            except Exception as er: errors.append(str(er))
        if before is not None:
            try:
                require(preserved_equal(before,state(s),phase),'protected services differ after rollback')
                for p,h in protected_before.items():require(digest(P(p).read_bytes())==h,'protected config differs after rollback')
            except Exception as er: errors.append(str(er))
        receipt.update(rollback_verified=bool(changed or stopped) and not errors,rollback_errors=errors)
        record(); raise

def main():
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest='action')
    prep=sub.add_parser('prepare'); prep.add_argument('--spec',required=True); prep.add_argument('--source',required=True); prep.add_argument('--bundle',required=True)
    for name in ('qualify','apply'):
        x=sub.add_parser(name); x.add_argument('--bundle',required=True); x.add_argument('--plan-sha256',required=True)
        if name=='apply': x.add_argument('--phase',choices=['controller','gatewaylink','terminal'],required=True)
    a=p.parse_args(); require(a.action is not None,'action required'); globals()[a.action](a)
if __name__=='__main__': main()
