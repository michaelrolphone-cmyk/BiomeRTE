#!/usr/bin/env python3
"""Run-owned Docker workspaces. All effects are serialized and journalled locally."""
import base64, contextlib, fcntl, gzip, hashlib, json, os, re, secrets, shutil, subprocess, sys, time, uuid
from pathlib import Path
ROOT=Path(os.environ.get('BIOME_WORKSPACE_HOME',Path.home()/'.local/share/biome-workspaces'))
GITROOT=Path.home()/'.local/share/biome-gitlab'
TERMINAL=Path.home()/'.local/share/biome-mac-terminal'
def digest(x):return hashlib.sha256(json.dumps(x,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def read(p):return json.loads(Path(p).read_text())
def save(p,x):
 p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);q=p.with_name(p.name+'.'+uuid.uuid4().hex);q.write_text(json.dumps(x,sort_keys=True));q.chmod(0o600)
 with q.open('rb') as f:os.fsync(f.fileno())
 os.replace(q,p)
def gateway():
 sys.path.insert(0,str(TERMINAL));import mac_provider;return mac_provider
def identifier(x):
 if not isinstance(x,str) or not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_@.-]{0,127}',x):raise ValueError('Invalid identifier')
 return x
@contextlib.contextmanager
def lock(name,blocking=True):
 p=ROOT/'locks'/identifier(name);p.parent.mkdir(parents=True,exist_ok=True)
 with p.open('a') as f:
  fcntl.flock(f,fcntl.LOCK_EX|(0 if blocking else fcntl.LOCK_NB));yield
def docker(*args,input=None,check=True):
 p=subprocess.run(['docker',*map(str,args)],input=input,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
 if check and p.returncode:raise RuntimeError(p.stderr.decode(errors='replace')[-3000:])
 return p
def exists(kind,name):return docker(kind,'inspect',name,check=False).returncode==0
def hashed(path):
 h=hashlib.sha256()
 with Path(path).open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()
def archive_image(image,path):
 path=Path(path)
 with gzip.open(str(path)+'.partial','wb',compresslevel=1) as out:
  p=subprocess.Popen(['docker','save',image],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
  shutil.copyfileobj(p.stdout,out);err=p.stderr.read();code=p.wait()
  if code:raise RuntimeError(err.decode())
 os.replace(str(path)+'.partial',path)
 return {'file':str(path),'sha256':hashed(path),'size':path.stat().st_size}
def load_image(a):
 if hashed(a['file'])!=a['sha256']:raise RuntimeError('Image archive checksum mismatch')
 with gzip.open(a['file'],'rb') as f:
  p=subprocess.Popen(['docker','load'],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
  shutil.copyfileobj(f,p.stdin);p.stdin.close();err=p.stderr.read()
  if p.wait():raise RuntimeError(err.decode())
def template_publish(a):
 t=a['template'];identifier(t['name']);identifier(str(t['version']))
 for k in ('base','dockerfile','platform','probe'):assert isinstance(t[k],str) and t[k],k
 if t['platform'] not in ('linux/arm64','linux/amd64'):raise ValueError('Unsupported current Mac platform')
 key=digest(t);p=ROOT/'templates'/(t['name']+'@'+str(t['version'])+'.json')
 with lock('templates'):
  if p.exists() and read(p)['revision']!=key:raise ValueError('Template version is immutable; publish a new version')
  save(p,dict(t,revision=key))
 return dict(t,revision=key)
def templates():
 result=[]
 for p in sorted((ROOT/'templates').glob('*.json')):
  t=read(p);s=ROOT/'images'/t['revision']/'state.json';build=read(s) if s.exists() else {'state':'unbuilt'}
  result.append(dict(t,build_state=build['state'],build_error=build.get('error','')))
 return result
def template(a):return read(ROOT/'templates'/(identifier(a.get('template','linux-basic@1'))+'.json'))
def ensure_image(a):
 t=template(a);key=t['revision'];d=ROOT/'images'/key;d.mkdir(parents=True,exist_ok=True)
 with lock('image-'+key):
  p=d/'state.json';prior=read(p) if p.exists() else {}
  if prior.get('state')=='ready':
   if not exists('image',prior['image']):load_image(prior['archive'])
   return dict(prior,reused=True)
  save(p,{'state':'building','revision':key,'started_at':time.time()})
  try:
   pin=d/'base.json'
   if not pin.exists():
    docker('pull','--platform',t['platform'],t['base']);info=json.loads(docker('image','inspect',t['base']).stdout)[0]
    save(pin,{'reference':info['RepoDigests'][0] if info.get('RepoDigests') else info['Id']})
   (d/'Dockerfile').write_text(t['dockerfile'].replace('${BASE}',read(pin)['reference']))
   tag='biome-workspace-template:'+key[:32]
   with (d/'build.log').open('wb') as log:
    proc=subprocess.run(['docker','build','--platform',t['platform'],'-t',tag,str(d)],stdout=log,stderr=subprocess.STDOUT)
   if proc.returncode:raise RuntimeError('Image build failed; read template build log with template.get')
   probe=docker('run','--rm','--platform',t['platform'],'--entrypoint','/bin/bash',tag,'-lc',t['probe'],check=False)
   (d/'probe.log').write_bytes(probe.stdout+b'\n'+probe.stderr)
   if probe.returncode:raise RuntimeError('Template validation failed: '+probe.stderr.decode(errors='replace')[-2000:])
   image=json.loads(docker('image','inspect',tag).stdout)[0]['Id'];archive=archive_image(image,d/'image.tar.gz')
   result={'state':'ready','revision':key,'image':image,'tag':tag,'platform':t['platform'],'archive':archive,'probe':probe.stdout.decode(errors='replace')[-4000:],'built_at':time.time(),'base':read(pin)['reference']};save(p,result);return dict(result,reused=False)
  except Exception as e:save(p,{'state':'failed','revision':key,'error':str(e)});raise
def state_path(wid):return ROOT/'instances'/identifier(wid)/'state.json'
def state(wid):return read(state_path(wid))
def persist(w):save(state_path(w['id']),w)
def run_state(run_id):return gateway().invoke('process.get',{'run':run_id})
def provision(a):
 run=run_state(a['run_id'])
 if run['state']!='running':raise ValueError('Workspace needs a running Biome process')
 wid='ws-'+digest([a['run_id'],a.get('name','main')])[:32]
 with lock(wid):
  if state_path(wid).exists():return state(wid)
  image=ensure_image(a);volume=wid+'-data';docker('volume','create','--label','biome.workspace='+wid,volume)
  w={'id':wid,'run_id':a['run_id'],'name':a.get('name','main'),'template':a.get('template','linux-basic@1'),'template_revision':image['revision'],'image':image['image'],'platform':image['platform'],'volume':volume,'container':wid,'state':'stored','generation':0,'holder':None,'checkpoint':None,'auto_suspend':a.get('auto_suspend',True),'gitlab_project':a.get('gitlab_project','biome-agent/biome-git-qualification'),'resources':a.get('resources',{}),'created_at':time.time()}
  persist(w);return w
def fence(w,a):
 if w['state']!='active' or w.get('holder')!=a.get('holder') or w['generation']!=a.get('generation'):raise ValueError('Stale or inactive workspace access')
 if w.get('active_operation'):raise RuntimeError('Uncertain prior command; reconcile before modifying workspace')
def start(w):
 if exists('container',w['container']):docker('rm','-f',w['container'])
 ca=GITROOT/'tls/gitlab.crt';host=__import__('urllib.parse',fromlist=['urlsplit']).urlsplit(read(GITROOT/'config.json')['url']).hostname
 args=['create','--name',w['container'],'--label','biome.workspace='+w['id'],'--platform',w['platform'],'--mount','type=volume,src='+w['volume']+',dst=/workspace','--mount','type=bind,src='+str(ca)+',dst=/run/biome/gitlab.crt,readonly','--tmpfs','/run/biome-secrets:mode=0700','--add-host',host+':host-gateway','-w','/workspace','--entrypoint','/bin/bash']
 for key,flag in [('memory','--memory'),('cpus','--cpus')]:
  if key in w['resources']:args.extend([flag,str(w['resources'][key])])
 args.extend([w['image'],'-lc','trap "exit 0" TERM INT; while true; do sleep 3600 & wait $!; done']);docker(*args);docker('start',w['container'])
 docker('exec',w['container'],'/bin/bash','-lc','mkdir -p /root/.config; cp /run/biome/gitlab.crt /usr/local/share/ca-certificates/biome-gitlab.crt; update-ca-certificates >/dev/null')
def git_env(w):
 sys.path.insert(0,str(GITROOT));import proxy
 token=proxy.token();host=__import__('urllib.parse',fromlist=['urlsplit']).urlsplit(proxy.settings()['url']).hostname
 # Credentials live only on tmpfs; the helper rejects other destinations.
 script='#!/bin/sh\n[ "$1" = get ] || exit 0\nprotocol=; host=\nwhile IFS="=" read -r k v; do case "$k" in protocol) protocol="$v";; host) host="$v";; esac; done\nhost=$(printf "%s" "$host" | tr "[:upper:]" "[:lower:]")\n[ "$protocol" = https ] && [ "$host" = "'+host+':8443" ] || exit 0\nprintf "username=oauth2\\npassword="\ncat /run/biome-secrets/token\nprintf "\\n\\n"\n'
 docker('exec','-i',w['container'],'sh','-c','umask 077; cat > /run/biome-secrets/token',input=token.encode())
 docker('exec','-i',w['container'],'sh','-c','cat > /run/biome-secrets/git-helper; chmod 700 /run/biome-secrets/git-helper',input=script.encode())
 return ['-c','credential.helper=','-c','credential.helper=!sh /run/biome-secrets/git-helper','-c','http.sslCAInfo=/run/biome/gitlab.crt','-c','user.name=Biome Agent','-c','user.email=biome-agent@biome.local']
def probe_git(w):
 url=read(GITROOT/'config.json')['url']+'/'+w['gitlab_project']+'.git'
 docker('exec',w['container'],'git',*git_env(w),'ls-remote',url)
def acquire(w,a):
 identifier(a['holder'])
 if w['state']=='disposed':raise ValueError('Workspace disposed')
 if w.get('active_operation'):raise RuntimeError('Reconcile interrupted command before acquisition')
 if w['state']=='active':
  if w['holder']!=a['holder']:raise ValueError('Workspace has another writer; use handoff')
  return w
 if w['checkpoint']:
  restore(w)
 elif not exists('image',w['image']):load_image(read(ROOT/'images'/w['template_revision']/'state.json')['archive'])
 start(w)
 try:probe_git(w)
 except Exception:docker('stop','-t','-1',w['container'],check=False);raise
 w.update(state='active',holder=a['holder'],generation=w['generation']+1);persist(w);return w
def checkpoint(w,operation):
 if w.get('active_operation'):raise RuntimeError('Cannot checkpoint an unresolved command')
 d=state_path(w['id']).parent/'checkpoints'/identifier(operation);d.mkdir(parents=True,exist_ok=True)
 was=w['state'];w['state']='checkpointing';persist(w)
 if exists('container',w['container']):
  docker('stop','-t','-1',w['container']);image=docker('commit',w['container']).stdout.decode().strip()
 else:image=w['image']
 archive=archive_image(image,d/'image.tar.gz')
 # All writers have stopped before volume snapshot; secrets were on tmpfs.
 vp=d/'volume.tar.gz'
 with vp.open('wb') as out:
  proc=subprocess.run(['docker','run','--rm','--platform',w['platform'],'--mount','type=volume,src='+w['volume']+',dst=/data,readonly','--entrypoint','tar',image,'-C','/data','-czf','-','.'],stdout=out,stderr=subprocess.PIPE)
  if proc.returncode:raise RuntimeError(proc.stderr.decode())
 cp={'id':operation,'image':image,'image_archive':archive,'volume_archive':{'file':str(vp),'sha256':hashed(vp),'size':vp.stat().st_size},'created_at':time.time()};save(d/'manifest.json',cp)
 w.update(checkpoint=cp,image=image,state='suspended',holder=None,generation=w['generation']+1);persist(w)
 # Both durable archives are verified before local runtime data is removed.
 docker('rm',w['container'],check=False);docker('volume','rm',w['volume']);return w
def restore(w):
 cp=w['checkpoint'];a=cp['volume_archive']
 if hashed(a['file'])!=a['sha256']:raise RuntimeError('Volume archive checksum mismatch')
 if exists('container',w['container']):docker('rm','-f',w['container'])
 if not exists('image',cp['image']):load_image(cp['image_archive'])
 if exists('volume',w['volume']):docker('volume','rm',w['volume'])
 docker('volume','create','--label','biome.workspace='+w['id'],w['volume'])
 with Path(a['file']).open('rb') as data:
  proc=subprocess.run(['docker','run','--rm','-i','--platform',w['platform'],'--mount','type=volume,src='+w['volume']+',dst=/data','--entrypoint','tar',cp['image'],'-C','/data','-xzf','-'],stdin=data,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
  if proc.returncode:raise RuntimeError(proc.stderr.decode())
 w['image']=cp['image']
def execute(w,a,operation,git=False):
 fence(w,a);argv=a.get('argv')
 if not isinstance(argv,list) or not argv or any(not isinstance(x,str) for x in argv):raise ValueError('argv must be a nonempty string array')
 cwd=a.get('cwd','/workspace')
 if cwd!='/workspace' and not cwd.startswith('/workspace/'):raise ValueError('cwd must be in /workspace')
 from pathlib import PurePosixPath
 if '..' in PurePosixPath(cwd).parts:raise ValueError('cwd escapes workspace')
 if git:argv=['git',*git_env(w),*argv]
 # Record before dispatch. A service crash leaves explicit unresolved state.
 w['active_operation']=operation;persist(w)
 env=[]
 if not git:
  # SDK environment is loaded per command, preserving arbitrary argv quoting.
  argv=['/bin/bash','-c','if [ -n "$IDF_PATH" ]; then . "$IDF_PATH/export.sh" >/dev/null; fi; exec "$@"','biome',*argv]
 p=docker('exec','-i','-w',cwd,w['container'],*argv,input=a.get('stdin','').encode(),check=False)
 w.pop('active_operation');w['last_operation']={'id':operation,'exit_code':p.returncode};persist(w)
 return {'exit_code':p.returncode,'stdout':p.stdout.decode(errors='replace'),'stderr':p.stderr.decode(errors='replace')}
def dispose(w):
 w.update(state='disposing',holder=None,generation=w['generation']+1);persist(w)
 docker('rm','-f',w['container'],check=False)
 if exists('container',w['container']):raise RuntimeError('Container removal pending')
 docker('volume','rm',w['volume'],check=False)
 if exists('volume',w['volume']):raise RuntimeError('Volume removal pending')
 private_images={read(p)['image'] for p in state_path(w['id']).parent.glob('checkpoints/*/manifest.json')}
 shared={read(p).get('image') for p in (ROOT/'images').glob('*/state.json')}
 for image in private_images-shared:docker('image','rm','-f',image,check=False)
 if any(exists('image',image) for image in private_images-shared):raise RuntimeError('Private checkpoint image removal pending')
 shutil.rmtree(state_path(w['id']).parent/'checkpoints',ignore_errors=True)
 w.update(state='disposed',checkpoint=None,disposed_at=time.time());w.pop('active_operation',None);persist(w);return w
def handle(tool,a,operation):
 if tool=='template.list':return {'templates':templates()}
 if tool=='template.publish':return template_publish(a)
 if tool=='template.ensure_image':return ensure_image(a)
 if tool=='template.get':
  t=template(a);d=ROOT/'images'/t['revision'];return {'template':t,'build':read(d/'state.json') if (d/'state.json').exists() else {'state':'unbuilt'},'build_log':(d/'build.log').read_text(errors='replace')[-12000:] if (d/'build.log').exists() else '', 'probe_log':(d/'probe.log').read_text(errors='replace')[-12000:] if (d/'probe.log').exists() else ''}
 if tool=='provision':
  if a.get('run_id')=='$current':
   runs=gateway().invoke('process.list',{})['runs'];matches=[]
   for r in runs:
    if r['state']=='running':
     actual=run_state(r['id'])
     if any(t.get('action')==operation for t in actual.get('tokens',[])):matches.append(r['id'])
   if len(matches)!=1:raise ValueError('Cannot resolve unique invoking BPMN run')
   a=dict(a,run_id=matches[0])
  return provision(a)
 if tool=='list':return {'workspaces':[read(p) for p in (ROOT/'instances').glob('*/state.json') if not a.get('run_id') or read(p)['run_id']==a['run_id']]}
 wid=identifier(a['workspace_id'])
 if tool=='status':return state(wid)
 with lock(wid):
  w=state(wid)
  if tool=='acquire':return acquire(w,a)
  if tool=='dispose':return dispose(w)
  if tool=='reconcile':
   # Stopping the entire container is the enforcement point for unresolved execs.
   docker('stop','-t','-1',w['container'],check=False)
   info=json.loads(docker('inspect',w['container']).stdout)[0]
   if info['State']['Running']:raise RuntimeError('Container is still running')
   w['reconciled_operation']=w.pop('active_operation',None);w['reconciliation_note']=a['reason'];persist(w)
   return checkpoint(w,operation)
  fence(w,a)
  if tool in ('checkpoint','suspend','handoff'):
   old=w['holder'];w=checkpoint(w,operation)
   if tool=='checkpoint':return acquire(w,{'holder':old})
   if tool=='handoff':w['handoff']=a.get('context',{});persist(w);return acquire(w,{'holder':a['to_holder']})
   return w
  if tool in ('exec','git'):return execute(w,a,operation,tool=='git')
  if tool in ('file.read','file.write'):
   code='import pathlib,sys,base64; p=(pathlib.Path("/workspace")/sys.argv[1]).resolve(); assert p.is_relative_to(pathlib.Path("/workspace")), "path escapes workspace"; '
   code+= 'print(base64.b64encode(p.read_bytes()).decode())' if tool=='file.read' else 'p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(base64.b64decode(sys.stdin.read(),validate=True))'
   return execute(w,dict(a,argv=['python3','-c',code,a['path']],stdin=a.get('data_base64','')),operation)
  raise ValueError('Unknown workspace operation')
def idle_wait(r):
 tokens=r.get('tokens',[])
 if not tokens or not all('wait' in t or 'delay_id' in t for t in tokens):return False
 waits={w['id']:w for w in gateway().invoke('process.waits',{'run':r['id']})} if any('wait' in t for t in tokens) else {}
 # A delegate wait represents an agent still doing work, not idle compute.
 return all('delay_id' in t or waits.get(t['wait'],{}).get('kind') in ('human','message') for t in tokens)
def monitor_once():
 for p in (ROOT/'instances').glob('*/state.json'):
  try:
   w=read(p)
   if w['state']=='disposed':continue
   r=run_state(w['run_id'])
   if r['state'] in ('completed','failed','cancelled'):
    children=[run_state(x)['state'] for x in r.get('children',[])]
    if not all(x in ('completed','failed','cancelled') for x in children):continue
    if w.get('active_operation'):docker('stop','-t','-1',w['container'],check=False)
   with lock(w['id'],False):
    w=read(p)
    if r['state'] in ('completed','failed','cancelled'):
     children=[run_state(x)['state'] for x in r.get('children',[])]
     if all(x in ('completed','failed','cancelled') for x in children):dispose(w)
    elif w['state']=='active' and w['auto_suspend'] and not w.get('active_operation') and idle_wait(r):checkpoint(w,'wait-'+uuid.uuid4().hex)
  except BlockingIOError:pass
  except Exception as e:save(ROOT/'monitor-errors'/ (p.parent.name+'.json'),{'error':str(e),'at':time.time()})
