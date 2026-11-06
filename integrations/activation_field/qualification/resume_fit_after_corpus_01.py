"""Restore the exact cooperatively paused fit when corpus01 releases its lock."""
import datetime, fcntl, json, sqlite3, sys, time
from pathlib import Path
ROOT=Path.home()/'.local/share/biome-activation-field'
SIDE=Path.home()/'.local/share/biome-context-reconstruction'
sys.path.insert(0,json.loads((SIDE/'source-version.json').read_text())['source'])
from activation_field.provider import gateway
g=gateway()
RUN='b3b31360dc2748f18fa55568dba076eb'
PAUSE='afctx-corpus01-fit-pause-20261106'
RESUME='afctx-corpus01-fit-resume-20261106'
OUT=ROOT/'subject-corpus-01-fit-guard.json'
RESOURCE='model.field-transform.mac.95b20447b68a57272f47'
def save(**data):
    data['observed_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
    tmp=OUT.with_suffix('.tmp');tmp.write_text(json.dumps(data,indent=2));tmp.replace(OUT)
def invoke(op,args,rid=None):
    while True:
        try:
            result=g.invoke(op,args,rid=rid,realm='site')
            if result.get('error_code'):raise RuntimeError(str(result))
            return result
        except Exception as exc:
            save(status='retrying',operation=op,error=str(exc))
            time.sleep(5)
def receipt(op):
    while True:
        r=invoke('cap.submission',dict(operation=op))
        if r.get('state') in ('succeeded','failed','cancelled','rejected','uncertain'):return r
        time.sleep(5)
def run():
    save(status='armed',pause_operation=PAUSE,fit_run_id=RUN)
    paused=receipt(PAUSE)
    result=paused.get('result',{}).get('result',{})
    if paused['state']!='succeeded' or not result.get('cancel_requested'):
        save(status='no_owned_pause',receipt=paused);return
    expected=result['version']+1
    save(status='waiting_for_corpus_exit',fit_run_id=RUN,resume_expected_version=expected)
    with (ROOT/'subject-corpus-01.lock').open('a+b') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        while True:
            db=sqlite3.connect('file:'+str(ROOT/'transforms/jobs.sqlite')+'?mode=ro',uri=True)
            row=db.execute('SELECT state,version FROM jobs WHERE id=?',(RUN,)).fetchone()
            db.close()
            if row[0]=='running' and row[1]==expected-1:
                time.sleep(5);continue
            if tuple(row)!=('cancelled',expected):
                save(status='no_resume_needed_or_ownership_changed',fit_state=list(row));return
            break
        args=dict(contract='model.field_transform.fit:1',resource=RESOURCE,operation=RESUME,
                  inputs=dict(arguments=dict(resume_run_id=RUN,expected_version=expected)))
        invoke('cap.submit',args,RESUME+'-submit')
        resumed=receipt(RESUME)
        save(status='resume_recorded' if resumed['state']=='succeeded' else 'resume_failed',receipt=resumed)
with (ROOT/'subject-corpus-01-fit-guard.lock').open('a+b') as guard:
    try:fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:sys.exit(0)
    run()
