"""Assemble a new private bundle from a verified base plus authored source delta.
Always rerun product tests when source changes; no production files are touched.
"""
import os,sys,json,hashlib,socket,platform,sqlite3,zlib,base64,subprocess,re,time
pkg=json.loads(zlib.decompress(base64.b64decode(''.join(sys.argv[1:]))).decode());base=pkg['base'];bundle=pkg['bundle']
def read(p):
 with open(p,'rb') as f:return f.read()
def sha(data):return hashlib.sha256(data).hexdigest()
def safe(name):
 assert not os.path.isabs(name) and '..' not in name.split('/') and name not in ('','.');return name
def write(p,data):
 os.makedirs(os.path.dirname(p),mode=0o700,exist_ok=True)
 with open(p,'wb') as f:f.write(data)
 os.chmod(p,0o600)
def tree(root):
 out={}
 for directory,dirs,names in os.walk(root):
  assert not any(os.path.islink(directory+'/'+x) for x in dirs+names)
  for name in names:out[os.path.relpath(directory+'/'+name,root)]=sha(read(directory+'/'+name))
 return out
oldplan=json.loads(read(base+'/plan.json').decode());plan=json.loads(pkg['files']['plan.json'])
assert sha(read(base+'/plan.json'))==pkg['base_plan_sha256']
assert platform.system()==plan['platform'] and socket.gethostname()==plan['hostname']
assert tree(base+'/fixture')==oldplan['fixture_hashes']
assert sha(pkg['files']['plan.json'].encode())==pkg['plan_sha256'] and not os.path.lexists(bundle)
assert plan['native_bundle']==bundle
helpers=set(plan['helper_hashes']) | {'retention_rollout.py'}
def validate_paths(x):
 if isinstance(x,str) and os.path.isabs(x) and os.path.basename(x) in helpers:assert os.path.dirname(x)==bundle
 elif isinstance(x,list):
  for i,v in enumerate(x):
   if v=='--bundle':assert i+1<len(x) and x[i+1]==bundle
   validate_paths(v)
 elif isinstance(x,dict):
  for v in x.values():validate_paths(v)
validate_paths(plan)
os.mkdir(bundle,0o700)
for name,data in pkg['files'].items():
 assert '/' not in name;safe(name);write(bundle+'/'+name,data.encode())
assert sha(read(bundle+'/retention_rollout.py'))==plan['runner_sha256']
for name,h in plan['helper_hashes'].items():assert sha(read(bundle+'/'+name))==h
for name,h in plan['fixture_hashes'].items():
 safe(name)
 data=pkg['fixture_delta'][name].encode() if name in pkg['fixture_delta'] else read(base+'/fixture/'+name)
 assert sha(data)==h;write(bundle+'/fixture/'+name,data)
assert tree(bundle+'/fixture')==plan['fixture_hashes']
for f in plan['files']:
 data=read(bundle+'/fixture/'+safe(pkg['payload_sources'][f['payload']]));assert sha(data)==f['new_sha256'];write(bundle+'/'+f['payload'],data)
for name in pkg['files']:
 if name.endswith('.py'):compile(read(bundle+'/'+name),name,'exec')
for name in ('test-home','test-tmp'):os.mkdir(bundle+'/'+name,0o700)
env={'PATH':'/usr/bin:/bin','HOME':bundle+'/test-home','TMPDIR':bundle+'/test-tmp','PYTHONDONTWRITEBYTECODE':'1','LANG':'C.UTF-8'}
def run(modules,cwd,path,count):
 with open(path,'wb') as output:
  p=subprocess.Popen([sys.executable,'-u','-B','-S','-m','unittest','-v']+modules,cwd=cwd,env=env,stdout=output,stderr=subprocess.STDOUT);code=p.wait()
 log=read(path)
 if code:
  print('FAILED_LOG_ZLIB_BASE64 '+base64.b64encode(zlib.compress(log)).decode(),flush=True)
  raise RuntimeError('Native qualification failed')
 assert re.search(('\nRan %d tests in '%count).encode(),log) and re.search(rb'\nOK\s*$',log) and b'skipped' not in log.lower()
 return log
sys.path.insert(0,bundle)
import retention_rollout
retention_rollout.verify_bundle_paths(plan,bundle)
proof=pkg.get('reuse_product_proof')
if proof:
 original=json.loads(read(base+'/qualification.json').decode())
 assert original['passed'] is True and original['plan_sha256']==pkg['base_plan_sha256']
 assert original['tests']==plan['minimum_tests'] and original['log_sha256']==proof['log_sha256']
 assert original['python']==sys.executable and original['python_version']==sys.version
 assert original['platform']==platform.system() and original['sqlite_version']==sqlite3.sqlite_version

 assert plan['fixture_hashes']==oldplan['fixture_hashes'] and plan['tests']==oldplan['tests'] and plan['minimum_tests']==oldplan['minimum_tests']
 assert sys.executable==proof['python'] and sys.version==proof['python_version']
 log=read(base+'/qualification.log');assert sha(log)==proof['log_sha256']
 assert re.search(('\nRan %d tests in '%plan['minimum_tests']).encode(),log) and re.search(rb'\nOK\s*$',log) and b'skipped' not in log.lower()
 write(bundle+'/qualification.log',log)
 write(bundle+'/product-proof.json',json.dumps(dict(proof,verified_fixture_files=len(plan['fixture_hashes']),source_plan_sha256=pkg['base_plan_sha256']),indent=2).encode())
else:
 log=run(plan['tests'],bundle+'/fixture',bundle+'/qualification.log',plan['minimum_tests'])
helperlog=run(pkg['helper_modules'],bundle,bundle+'/native-helper-tests.log',pkg['helper_tests'])
sys.path.insert(0,bundle)
import terminal_safety
assert terminal_safety.process_snapshot(platform.system())['processes']
q={'passed':True,'tests':plan['minimum_tests'],'plan_sha256':pkg['plan_sha256'],'platform':platform.system(),'hostname':socket.gethostname(),'python':sys.executable,'python_version':sys.version,'sqlite_version':sqlite3.sqlite_version,'time':time.time(),'log_sha256':sha(log),'native_helper_tests':pkg['helper_tests'],'helper_log_sha256':sha(helperlog),'native_process_snapshot_verified':True,'provenance':{'fresh_native_product_tests':not bool(proof),'retained_product_proof':proof,'source_files_verified':len(plan['fixture_hashes']),'modules':plan['tests'],'base_plan_sha256':pkg['base_plan_sha256']}}
write(bundle+'/qualification.json',json.dumps(q,indent=2).encode());print(json.dumps(q),flush=True)
