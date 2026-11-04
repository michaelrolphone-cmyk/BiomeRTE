"""Apply an installed provider update only after its existing work settles."""
import json
import os
from pathlib import Path
import plistlib
import sqlite3
import subprocess
import sys
import time

def main():
    state, service, source = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    status = state/'transport-update.json'
    if status.exists() and json.loads(status.read_text()).get('state') == 'applied':
        return
    with sqlite3.connect(str(state/'worker.sqlite')) as db:
        active=db.execute("SELECT count(*) FROM actions WHERE state='uncertain'").fetchone()[0]
    if active:
        status.write_text(json.dumps({'state':'waiting_for_existing_operation','active':active}))
        return
    plist=Path.home()/'Library/LaunchAgents'/(service+'.plist')
    old=plist.read_bytes()
    config=plistlib.loads(old)
    config['ProgramArguments'][1]=str(source)
    plist.write_bytes(plistlib.dumps(config))
    domain='gui/'+str(os.getuid())
    subprocess.run(['launchctl','bootout',domain+'/'+service],capture_output=True)
    for attempt in range(5):
        time.sleep(.5)
        result=subprocess.run(['launchctl','bootstrap',domain,str(plist)],capture_output=True)
        if result.returncode==0:
            status.write_text(json.dumps({'state':'applied','source':str(source)}))
            return
    # A failed reload restores the known prior startup configuration.
    plist.write_bytes(old)
    subprocess.run(['launchctl','bootstrap',domain,str(plist)],capture_output=True)
    status.write_text(json.dumps({'state':'reload_failed_previous_configuration_restored'}))
    raise RuntimeError('Provider reload failed')

if __name__=='__main__':
    main()
