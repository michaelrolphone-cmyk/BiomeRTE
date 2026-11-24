"""Independent owner maintenance agent. No imports or calls into BiomeRTE."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.request import Request, urlopen

os.umask(0o077)
HOME = Path(sys.argv[1])
CONF = json.loads((HOME / 'config.json').read_text())
KEY = (HOME / 'device.key').read_text().strip()
DB = sqlite3.connect(str(HOME / 'ledger.sqlite'), check_same_thread=False)
DB.execute('PRAGMA journal_mode=WAL')
DB.execute('CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY,state TEXT,result TEXT)')
DB.execute("UPDATE commands SET state='uncertain',result=? WHERE state='running'", (json.dumps({'state':'uncertain','reason':'Maintenance agent restarted; reconcile process and output files before another execution.'}),))
DB.commit()
LOCK = threading.RLock()


# Jobs are admitted by the authenticated maintenance gateway. The device
# credential and TLS transport remain required; no separate phone signature.

def exchange(body):
    request = Request(CONF['origin'] + '/maintenance/device', json.dumps(body).encode(),
                      {'Authorization':'Bearer ' + KEY, 'Content-Type':'application/json',
                       'User-Agent':'BiomeRTE-Independent-Maintenance/1'})
    with urlopen(request) as response:
        return json.loads(response.read())


def execute(job):
    directory = HOME / 'commands' / hashlib.sha256(job['id'].encode()).hexdigest()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with (directory/'stdout').open('wb') as stdout, (directory/'stderr').open('wb') as stderr:
            process = subprocess.Popen(job['argv'], cwd=job.get('cwd') or None,
                                       stdout=stdout, stderr=stderr, start_new_session=True)
            (directory/'pid').write_text(str(process.pid))
            code = process.wait()
        result = {'state':'succeeded' if code == 0 else 'failed', 'exit_code':code,
                  'stdout':(directory/'stdout').read_text(errors='replace'),
                  'stderr':(directory/'stderr').read_text(errors='replace'),
                  'output_directory':str(directory)}
    except Exception as error:
        result = {'state':'failed','error':type(error).__name__ + ': ' + str(error),
                  'output_directory':str(directory)}
    save_result(job['id'], result)


def save_result(identifier, result):
    with LOCK:
        DB.execute('UPDATE commands SET state=?,result=? WHERE id=?',
                   (result['state'],json.dumps(result),identifier))
        DB.commit()


def start_job(job):
    with LOCK:
        known = DB.execute('SELECT state FROM commands WHERE id=?',(job['id'],)).fetchone()
        if known:
            return False
        DB.execute("INSERT INTO commands VALUES(?,'running',NULL)",(job['id'],))
        DB.commit()
    try:
        threading.Thread(target=execute,args=(job,),daemon=True).start()
    except Exception as error:
        save_result(job['id'], {'state':'failed',
                    'error':'Maintenance worker could not start: ' + type(error).__name__})
        return False
    return True


while True:
    try:
        with LOCK:
            completed = list(DB.execute("SELECT id,result FROM commands WHERE state IN ('succeeded','failed','uncertain')"))
        for identifier, result in completed:
            exchange({'operation':'complete','id':identifier,'result':json.loads(result)})
            with LOCK:
                DB.execute("UPDATE commands SET state='reported' WHERE id=?",(identifier,))
                DB.commit()
        jobs = exchange({'operation':'poll'})['jobs']
        for job in jobs:
            start_job(job)
    except Exception as error:
        # Never print credentials, request bodies or command output to launchd logs.
        print('maintenance transport retry: '+type(error).__name__,flush=True)
    time.sleep(1)
