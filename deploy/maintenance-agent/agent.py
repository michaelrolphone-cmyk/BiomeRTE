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
import output_retention as retention

os.umask(0o077)
HOME = Path(sys.argv[1])
CONF = json.loads((HOME / 'config.json').read_text())
KEY = (HOME / 'device.key').read_text().strip()
DB = sqlite3.connect(str(HOME / 'ledger.sqlite'), check_same_thread=False)
DB.execute('PRAGMA journal_mode=WAL')
DB.execute('CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY,state TEXT,result TEXT)')
retention.initialize(DB)
LIMITS = retention.policy(CONF.get('output_retention'))
retention.recover_running(DB)
LOCK = threading.RLock()


def exchange(body):
    request = Request(CONF['origin'] + '/maintenance/device', json.dumps(body).encode(),
                      {'Authorization':'Bearer ' + KEY, 'Content-Type':'application/json',
                       'User-Agent':'BiomeRTE-Independent-Maintenance/1'})
    with urlopen(request) as response:
        return json.loads(response.read())


def execute(job):
    directory = HOME / 'commands' / hashlib.sha256(job['id'].encode()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    process = None
    quiescent = False
    try:
        with (directory/'stdout').open('wb') as stdout, (directory/'stderr').open('wb') as stderr:
            process = subprocess.Popen(job['argv'], cwd=job.get('cwd') or None,
                                       stdout=stdout, stderr=stderr, start_new_session=True)
            (directory/'pid').write_text(str(process.pid))
            code = process.wait()
            quiescent = retention.process_group_finished(process.pid)
        result = {'state':'succeeded' if code == 0 else 'failed', 'exit_code':code,
                  'stdout':(directory/'stdout').read_text(errors='replace'),
                  'stderr':(directory/'stderr').read_text(errors='replace'),
                  'output_directory':str(directory)}
    except Exception as error:
        quiescent = process is None or (process.poll() is not None and retention.process_group_finished(process.pid))
        result = {'state':'failed','error':type(error).__name__ + ': ' + str(error),
                  'output_directory':str(directory)}
    with LOCK:
        retention.complete(DB, job['id'], result, quiescent, home=HOME)


next_retention = 0.0
while True:
    try:
        with LOCK:
            completed = list(DB.execute("SELECT id,result FROM commands WHERE state IN ('succeeded','failed','uncertain')"))
        for identifier, result in completed:
            sent = json.loads(result)
            receipt = exchange({'operation':'complete','id':identifier,'result':sent})
            with LOCK:
                retention.acknowledge(DB, identifier, sent, receipt, CONF['host'])
        if time.monotonic() >= next_retention:
            with LOCK:
                try:
                    retention.trim(DB, HOME, LIMITS)
                except (OSError, ValueError, sqlite3.Error) as error:
                    print('maintenance retention retry: '+type(error).__name__, flush=True)
            next_retention = time.monotonic() + 60
        jobs = exchange({'operation':'poll'})['jobs']
        for job in jobs:
            with LOCK:
                if not retention.admit(DB, job):
                    continue
            threading.Thread(target=execute,args=(job,),daemon=True).start()
    except Exception as error:
        # Never print credentials, request bodies or command output to launchd logs.
        print('maintenance transport retry: '+type(error).__name__,flush=True)
    time.sleep(1)
