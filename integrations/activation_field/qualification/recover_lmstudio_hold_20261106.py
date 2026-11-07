"""Release only the observed LM Studio adapter hold; retain admission state and work."""
import datetime, fcntl, json, os, sqlite3, subprocess, time
from pathlib import Path
BASE=Path.home()/'.local/share'
RUNTIME=BASE/'biome-model-runtime'
FIELD=BASE/'biome-activation-field'
OWNER='e3bdf3c8bfe04c748b0b77d358ed53d1'
PID=64911
BIRTH='Wed Nov  4 21:04:19 2026'
SERVICE='gui/'+str(os.getuid())+'/org.biomerte.agent.lmstudio'
OUT=FIELD/'lmstudio-hold-recovery-20261106.json'
def queue():
    db=sqlite3.connect('file:'+str(RUNTIME/'admission.sqlite')+'?mode=ro',uri=True)
    db.row_factory=sqlite3.Row
    try:return [dict(r) for r in db.execute('SELECT * FROM queue ORDER BY ticket')]
    finally:db.close()
def evidence():
    result={'queue':queue(),'observed_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
    for name in ['subject-corpus-01.json','subject-corpus-01-fit-guard.json']:
        p=FIELD/name
        if p.exists():
            d=json.loads(p.read_text())
            result[name]={k:d[k] for k in ('status','stage','observed_at','waiting_for','capability_state','run_id','raw_reconstructed_text','error') if k in d}
    return result
def save(record):
    tmp=OUT.with_suffix('.tmp')
    tmp.write_text(json.dumps(record,indent=2));tmp.chmod(0o600);tmp.replace(OUT)
record={'before':evidence(),'action':'not_taken','reason':None}
save(record)
rows=record['before']['queue']
match=bool(rows and rows[0]['id']==OWNER and rows[0]['pid']==PID and rows[0]['birth']==BIRTH and rows[0]['phase']=='running')
if not match:
    record['reason']='Original hold is no longer the current model owner'
else:
    birth=subprocess.run(['/bin/ps','-p',str(PID),'-o','lstart='],capture_output=True,text=True).stdout.strip()
    command=subprocess.run(['/bin/ps','-p',str(PID),'-o','command='],capture_output=True,text=True).stdout.strip()
    service=subprocess.run(['/bin/launchctl','print',SERVICE],capture_output=True,text=True)
    service_pid=next((line.split('=',1)[1].strip() for line in service.stdout.splitlines() if line.strip().startswith('pid =')),None)
    record['identity']={'birth':birth,'service_pid':service_pid,'adapter_path_matches':str(BASE/'biome-lmstudio/lmstudio_agent.py') in command}
    if birth!=BIRTH or service_pid!=str(PID) or not record['identity']['adapter_path_matches']:
        record['reason']='Process or service identity changed; no restart'
    else:
        with (RUNTIME/'active.lock').open('a+b') as lock:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                fcntl.flock(lock,fcntl.LOCK_UN)
                locked=False
            except BlockingIOError:locked=True
        if not locked:
            record['reason']='Model lock is already free; no restart'
        else:
            # Recheck the exact head after gathering process evidence.
            current=queue()
            if not current or current[0]!=rows[0]:
                record['reason']='Model owner changed during inspection; no restart'
            else:
                record['action']='restart_requested';save(record)
                restart=subprocess.run(['/bin/launchctl','kickstart','-k',SERVICE],capture_output=True,text=True)
                record['restart_exit_code']=restart.returncode
                record['restart_stderr']=restart.stderr
                record['action']='adapter_restarted' if restart.returncode==0 else 'restart_failed'
                record['reason']='User requested freeing the blocking background LM Studio model hold'
                save(record)
record['after']=evidence()
save(record)
if record['action']=='adapter_restarted':
    time.sleep(5)
    record['after_settle']=evidence();save(record)
print(json.dumps(record))
