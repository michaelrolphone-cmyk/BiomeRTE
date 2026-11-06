"""Temporarily yield LM Studio background admission until corpus01 exits."""
import datetime,fcntl,json,os,plistlib,sqlite3,subprocess,sys,time
from pathlib import Path
ROOT=Path.home()/'.local/share/biome-activation-field'
MODEL=Path.home()/'.local/share/biome-model-runtime'
DOMAIN='gui/'+str(os.getuid())
SERVICE=DOMAIN+'/org.biomerte.agent.lmstudio'
OUT=ROOT/'lmstudio-corpus-pause-20261106.json'
def queue():
 db=sqlite3.connect('file:'+str(MODEL/'admission.sqlite')+'?mode=ro',uri=True)
 db.row_factory=sqlite3.Row
 try:return [dict(r) for r in db.execute('SELECT * FROM queue ORDER BY ticket')]
 finally:db.close()
def save(data):
 data['observed_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
 p=OUT.with_suffix('.tmp');p.write_text(json.dumps(data,indent=2));p.chmod(0o600);p.replace(OUT)
plists=[]
for p in (Path.home()/'Library/LaunchAgents').glob('*.plist'):
 try:
  cfg=plistlib.loads(p.read_bytes())
  if cfg.get('Label')=='org.biomerte.agent.lmstudio':plists.append(p)
 except Exception:pass
assert len(plists)==1,'Unique installed LM Studio adapter LaunchAgent required'
check=subprocess.run(['/bin/launchctl','print',SERVICE],capture_output=True,text=True)
pid=next((line.split('=',1)[1].strip() for line in check.stdout.splitlines() if line.strip().startswith('pid =')),None)
assert pid=='64911','Adapter PID changed; re-inspect before pausing'
birth=subprocess.check_output(['/bin/ps','-p',pid,'-o','lstart='],text=True).strip()
assert birth=='Wed Nov  4 21:04:19 2026','Adapter process identity changed'
cmd=subprocess.check_output(['/bin/ps','-p',pid,'-o','command='],text=True)
assert str(Path.home()/'.local/share/biome-lmstudio/lmstudio_agent.py') in cmd
with (ROOT/'subject-corpus-01.lock').open('a+b') as corpus:
 try:
  fcntl.flock(corpus,fcntl.LOCK_EX|fcntl.LOCK_NB)
  print(json.dumps({'action':'not_taken','reason':'Corpus supervisor has exited'}));sys.exit(0)
 except BlockingIOError:pass
record={'service':SERVICE,'domain':DOMAIN,'plist':str(plists[0]),'before':queue(),'action':'prepared','source_pid':pid}
save(record)
helper="import datetime,fcntl,json,os,subprocess,time\nfrom pathlib import Path\nroot=Path.home()/'.local/share/biome-activation-field'\nstate=root/'lmstudio-corpus-resume-20261106.json'\ndef save(**x):\n x['observed_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()\n p=state.with_suffix('.tmp');p.write_text(json.dumps(x));p.chmod(0o600);p.replace(state)\nwith (root/'lmstudio-corpus-resume.lock').open('a+b') as guard:\n fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)\n save(status='armed',pid=os.getpid())\n with (root/'lmstudio-corpus-handoff.lock').open('a+b') as gate:\n  fcntl.flock(gate,fcntl.LOCK_EX)\n with (root/'subject-corpus-01.lock').open('a+b') as corpus:\n  save(status='waiting_for_corpus_exit')\n  fcntl.flock(corpus,fcntl.LOCK_EX)\n  cfg=json.loads((root/'lmstudio-corpus-pause-20261106.json').read_text())\n  while True:\n   found=subprocess.run(['/bin/launchctl','print',cfg['service']],capture_output=True,text=True)\n   if found.returncode==0:\n    save(status='service_available',service=cfg['service']);break\n   result=subprocess.run(['/bin/launchctl','bootstrap',cfg['domain'],cfg['plist']],capture_output=True,text=True)\n   save(status='restore_requested',exit_code=result.returncode,stderr=result.stderr)\n   time.sleep(2)\n"
with (ROOT/'lmstudio-corpus-handoff.lock').open('a+b') as gate:
 fcntl.flock(gate,fcntl.LOCK_EX)
 with (ROOT/'lmstudio-corpus-resume.log').open('ab',buffering=0) as log:
  child=subprocess.Popen([sys.executable,'-S','-c',helper],stdout=log,stderr=log,start_new_session=True)
 ready=ROOT/'lmstudio-corpus-resume-20261106.json'
 while True:
  if child.poll() is not None:raise RuntimeError('Resumption guard failed before pause')
  if ready.exists():
   state=json.loads(ready.read_text())
   if state.get('pid')==child.pid and state.get('status')=='armed':break
  time.sleep(.1)
 assert child.poll() is None,'Resumption guard exited before pause'
 record['resume_guard_pid']=child.pid;save(record)
 result=subprocess.run(['/bin/launchctl','bootout',SERVICE],capture_output=True,text=True)
 record.update(action='adapter_paused' if result.returncode==0 else 'pause_failed',exit_code=result.returncode,stderr=result.stderr)
 save(record)
time.sleep(5)
record['after']=queue();save(record)
print(json.dumps(record))
