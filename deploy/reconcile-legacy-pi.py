"""Reconcile legacy Pi outcomes from local journals without executing commands."""
import os,sys,json,sqlite3,time,subprocess,hashlib,imp
HOME='/home/pi/.local/share'
LIVE=HOME+'/biomerte-releases/bounded-stream-bcef0dad6f8c'
sys.path.insert(0,LIVE)
from biomerte.worker import Remote
from biomerte.store import digest,encode
cfg=json.load(open(HOME+'/biomerte/node.json'))
admin=Remote('http://127.0.0.1:'+str(cfg['port']),cfg['site'],cfg['node'],None,token=cfg['admin_token'])
boot=open('/proc/sys/kernel/random/boot_id').read().strip()
terminal=imp.load_source('retained_terminal',HOME+'/biomerte-terminal/terminal.py')

def main():
    items=admin.call('cap.recovery.list',{'limit':64})['items']
    for name in ('biomerte-terminal','biomerte-docker'):
        directory=HOME+'/'+name;node=json.load(open(directory+'/node.json'))
        remote=Remote('http://127.0.0.1:'+str(cfg['port']),cfg['site'],node['node'],directory+'/node.key')
        db=sqlite3.connect(directory+'/worker.sqlite',timeout=30,isolation_level=None)
        db.execute('CREATE TABLE IF NOT EXISTS legacy_custody_audit(id TEXT PRIMARY KEY,identity TEXT,proof TEXT,outcome TEXT,controller_ack TEXT,observed REAL)')
        unit=name+'.service'
        status=subprocess.check_output(['systemctl','show',unit,'-p','KillMode','-p','MainPID']).decode()
        # Legacy commands had no detached runner; a completed control-group restart
        # is an executor fence. Daemon-side effects remain explicitly unknown.
        fence_ok='KillMode=control-group' in status and 'MainPID=0' not in status
        for item in items:
            if item['provider']!=node['node'] or item['state']!='historical_uncertainty':continue
            op=item['operation']
            a=remote.call('cap.status',{'operation':op,'recovery':True})
            identity=digest([a[k] for k in ('contract','inputs','resource','generation','lease')])
            local=db.execute('SELECT signature,state,result FROM actions WHERE id=?',(op,)).fetchone()
            if local and local[0]!=identity:raise RuntimeError('Local identity mismatch '+op)
            old=db.execute('SELECT identity,proof,outcome FROM legacy_custody_audit WHERE id=?',(op,)).fetchone()
            evidence={'service':unit,'service_observation':status,'boot_id':boot}
            if old:
                if old[0]!=identity:raise RuntimeError('Audit identity conflict '+op)
                proof=json.loads(old[1]);out=json.loads(old[2])
            else:
                native=None
                if name=='biomerte-terminal':
                    j=sqlite3.connect(directory+'/terminal.sqlite')
                    try:native=j.execute('SELECT body,state,result FROM jobs WHERE id=?',(op,)).fetchone()
                    finally:j.close()
                if native:
                    body=json.loads(native[0]);result=json.loads(native[2])
                    if native[1]!='finished' or result.get('quiescent') is not True:
                        print('UNSETTLED',op,flush=True);continue
                    argv=body['argv'];expected=a['inputs']['argv']
                    if argv[1:]!=expected[1:] or os.path.realpath(argv[0])!=os.path.realpath(expected[0]):
                        raise RuntimeError('Native argv mismatch '+op)
                    evidence.update(native_state=native[1],native_result=result,native_body_digest=digest(body))
                    method='process_identity'
                    out={'exit_code':result['exit_code'],'stdout':'','stderr':'','streams':{},
                         'evidence':dict(result,recovered_from='native_terminal_journal')}
                    out['evidence'].pop('stdout',None);out['evidence'].pop('stderr',None)
                    if '--apply' in sys.argv and all(os.path.isfile(result[k]) for k in ('stdout','stderr')):
                        for stream in ('stdout','stderr'):
                            out[stream],out['streams'][stream]=terminal.store_stream(result[stream],remote)
                    elif not all(os.path.isfile(result[k]) for k in ('stdout','stderr')):
                        out.update(reason='completion_recovered_output_unavailable',original_exit_code=result['exit_code'])
                        out['evidence']['output_unavailable']=True
                else:
                    if not fence_ok:raise RuntimeError('Legacy control-group fence unavailable '+op)
                    # Check the complete legacy provider cgroup for old executors.
                    pid=int(status.split('MainPID=')[1].splitlines()[0])
                    group=open('/proc/'+str(pid)+'/cgroup').read()
                    group_path=[l.split(':',2)[2] for l in group.splitlines() if ':name=systemd:' in l]
                    if not group_path:raise RuntimeError('No systemd custody group')
                    pids=open('/sys/fs/cgroup/systemd'+group_path[0]+'/cgroup.procs').read().split()
                    main_start=int(open('/proc/'+str(pid)+'/stat').read().rsplit(')',1)[1].split()[19])
                    processes=[]
                    for p in pids:
                        try:cmd=open('/proc/'+p+'/cmdline','rb').read().replace(b'\0',b' ').decode('utf8','replace')
                        except OSError:continue
                        start=int(open('/proc/'+p+'/stat').read().rsplit(')',1)[1].split()[19])
                        processes.append({'pid':int(p),'start_ticks':start})
                        if start < main_start and ('docker_cli.py' in cmd or '--supervise' in cmd):
                            raise RuntimeError('Unsettled legacy executor in provider group')
                    evidence.update(provider_pid=pid,cgroup=group_path[0],processes=processes,
                                    native_record_absent=True,scope='command executor; external effects unknown')
                    method='provider_restart_fence'
                    out={'reason':'legacy_executor_fenced_outcome_unknown','effects_unknown':True,
                         'replay_allowed':False,'evidence':{'quiescent':True,'executor_fenced':True}}
                proof={'identity_digest':identity,'quiescent':True,'method':method,'evidence':evidence}
            if '--apply' not in sys.argv:
                print('READY',op,proof['method'],out.get('exit_code'),flush=True);continue
            # Save exact proof/outcome before network mutation; an interrupted
            # retry reuses this immutable evidence and never runs original argv.
            db.execute('INSERT OR IGNORE INTO legacy_custody_audit VALUES(?,?,?,?,?,?)',
                       (op,identity,encode(proof),encode(out),'',time.time()))
            ack=remote.call('provider.reconcile',dict(provider=node['node'],operation=op,generation=a['generation'],state='failed' if out.get('effects_unknown') or out.get('reason') else 'succeeded',result=out,proof=proof))
            state='failed' if out.get('effects_unknown') or out.get('reason') else 'succeeded'
            if ack.get('id')!=op or ack.get('provider')!=node['node'] or ack.get('generation')!=a['generation'] or ack.get('result')!=out or (ack.get('state')!=state and ack.get('execution_state')!=state):
                raise RuntimeError('Reconciliation acknowledgement mismatch '+op)
            with db:
                if local:
                    db.execute('INSERT OR IGNORE INTO reconciliations VALUES(?,?,?,?,?)',(op,local[2],state,encode(out),time.time()))
                    db.execute('UPDATE actions SET state=?,result=? WHERE id=? AND signature=?',(state,encode(out),op,identity))
                db.execute('UPDATE legacy_custody_audit SET controller_ack=? WHERE id=?',(encode(ack),op))
            print('RECONCILED',op,state,'exit',out.get('exit_code'),flush=True)
        db.close()

if __name__=='__main__':main()
