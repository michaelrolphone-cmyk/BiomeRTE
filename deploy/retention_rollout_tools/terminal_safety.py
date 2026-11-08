"""Read-only terminal quiescence proof, including uncertain local executions."""
import os,json,sqlite3,hashlib,subprocess,time,calendar,fcntl,stat,math

def process_snapshot(system):
    env={'PATH':'/usr/bin:/bin','LC_ALL':'C','TZ':'UTC'}
    rows=subprocess.check_output(['/bin/ps','-axo','pid=,ppid=,pgid=,lstart=,state='],env=env).decode().splitlines()
    processes={}
    for row in rows:
        p=row.split()
        if len(p)<9:raise RuntimeError('Incomplete process snapshot')
        pid,ppid,pgid=map(int,p[:3]);started=calendar.timegm(time.strptime(' '.join(p[3:8]),'%a %b %d %H:%M:%S %Y'))
        record={'pid':pid,'ppid':ppid,'pgid':pgid,'started':started,'state':p[8]}
        if system=='Linux':
            try:
                with open('/proc/'+str(pid)+'/stat') as f:stat=f.read()
                record['start_ticks']=int(stat[stat.rfind(')')+2:].split()[19])
                with open('/proc/'+str(pid)+'/cgroup') as f:groups=f.read().splitlines()
                record['service_cgroup']=next((line.split(':',2)[2] for line in groups if line.split(':',2)[1]=='name=systemd'),None)
            except FileNotFoundError:continue
        processes[pid]=record
    boot=None
    if system=='Linux':
        with open('/proc/sys/kernel/random/boot_id') as f:boot=f.read().strip()
    return {'processes':processes,'boot_id':boot}

def process_reason(identity,evidence,saved,snapshot,system):
    if evidence.get('quiescent') is not True:return 'missing explicit quiescent evidence'
    if evidence.get('executed') is False:return None
    if not isinstance(identity,dict) or not identity.get('pid'):return 'missing execution PID identity'
    pid=int(identity['pid']);pgid=int(identity.get('pgid',pid));live=snapshot['processes'].get(pid)
    if system=='Linux':
        kernel=identity.get('kernel_identity')
        if not isinstance(kernel,dict) or kernel.get('pid')!=pid or 'boot_id' not in kernel or 'start_ticks' not in kernel:return 'missing Linux boot/start identity'
        if kernel['boot_id']!=snapshot['boot_id']:return None
        if live and live.get('start_ticks')!=int(kernel['start_ticks']):return None
    else:
        started=saved.get('started_at');duration=evidence.get('duration_seconds')
        if live:
            if not isinstance(started,(int,float)) or not isinstance(duration,(int,float)):return 'missing Mac execution time identity for live PID'
            if live['started']>started+duration+5:return None
    if live and not live['state'].startswith('Z'):return 'recorded execution PID still live'
    if any(p['pgid']==pgid and not p['state'].startswith('Z') for p in snapshot['processes'].values()):return 'recorded execution process group still live'
    return None

def readonly(path):
    if not os.path.isfile(path) or os.path.islink(path):raise RuntimeError('Missing or unsafe ledger '+os.path.basename(path))
    return sqlite3.connect('file:'+path+'?mode=ro',uri=True,timeout=5)

def file_stamp(value):
    return (value.st_dev,value.st_ino,value.st_mode,value.st_size,value.st_mtime_ns,value.st_ctime_ns)

def readonly_file(path):
    # No following links or blocking on a substituted FIFO. Recheck the opened
    # inode against its pathname so replacing the lock cannot bypass flock.
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        value=os.fstat(fd)
        if not stat.S_ISREG(value.st_mode) or file_stamp(value)!=file_stamp(os.lstat(path)):
            raise RuntimeError('Missing, changed or unsafe receipt file '+os.path.basename(path))
        result=os.fdopen(fd,'rb');fd=None;return result
    finally:
        if fd is not None:os.close(fd)

def mac_inventory(root):
    value=os.lstat(root)
    if not stat.S_ISDIR(value.st_mode):raise RuntimeError('Missing or unsafe terminal receipt directory')
    entries=[]
    for name in sorted(os.listdir(root)):
        value=os.lstat(root+'/'+name)
        if stat.S_ISLNK(value.st_mode):raise RuntimeError('Symlink in terminal receipt directory')
        entries.append((name,file_stamp(value)))
    return file_stamp(os.lstat(root)),entries

def mac_receipt(directory):
    path=directory+'/receipt.json';name=os.path.basename(directory)
    with readonly_file(path) as receipt:saved=json.loads(receipt.read().decode('utf-8'))
    op=saved.get('operation')
    if not isinstance(op,str) or hashlib.sha256(op.encode()).hexdigest()!=name:raise RuntimeError('Receipt operation identity mismatch')
    lockpath=directory+'/lock'
    if not os.path.isfile(lockpath) or os.path.islink(lockpath):return op,saved,None,'receipt lock missing or unsafe'
    # At most one lock and one receipt descriptor are held, regardless of count.
    with readonly_file(lockpath) as lock:
        try:fcntl.flock(lock,fcntl.LOCK_SH|fcntl.LOCK_NB)
        except OSError:return op,saved,None,'receipt locked by active adapter'
        lock_stamp=file_stamp(os.fstat(lock.fileno()))
        with readonly_file(path) as receipt:
            before=file_stamp(os.fstat(receipt.fileno()));data=receipt.read()
            if before!=file_stamp(os.fstat(receipt.fileno())) or before!=file_stamp(os.lstat(path)):
                raise RuntimeError('Receipt changed during locked read')
        saved=json.loads(data.decode('utf-8'))
        if saved.get('operation')!=op:raise RuntimeError('Receipt changed identity')
        if lock_stamp!=file_stamp(os.lstat(lockpath)):raise RuntimeError('Receipt lock changed during read')
        return op,saved,(before,lock_stamp,hashlib.sha256(data).hexdigest()),None

def reconciled_rows(worker):
    if not worker.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reconciliations'").fetchone():return {}
    rows=list(worker.execute('SELECT a.id,a.signature,a.state,a.result,r.previous_result,r.state,r.result,r.observed '
                             'FROM actions a JOIN reconciliations r ON r.id=a.id'))
    if len({row[0] for row in rows})!=len(rows):raise RuntimeError('Ambiguous duplicate reconciliation adoption')
    return {row[0]:row for row in rows}

def adopted_reason(row):
    op,signature,state,result,previous,audit_state,audit_result,observed=row
    if not isinstance(op,str) or not op:return 'invalid reconciled operation identity'
    if not isinstance(signature,str) or len(signature)!=64 or any(c not in '0123456789abcdef' for c in signature):
        return 'invalid reconciled action signature'
    if state not in ('succeeded','failed') or audit_state!=state:return 'reconciliation audit state differs'
    current=json.loads(result);audit=json.loads(audit_result);prior=json.loads(previous)
    if not all(isinstance(x,dict) for x in (current,audit,prior)):return 'invalid reconciliation audit result'
    if current!=audit:return 'reconciliation audit result differs'
    if type(observed) not in (int,float) or not math.isfinite(observed) or not 0<observed<=time.time():return 'invalid reconciliation observation time'
    if not (current.get('reconciliation',{}).get('quiescent') is True or current.get('evidence',{}).get('quiescent') is True):
        return 'reconciliation lacks explicit quiescent evidence'
    # The audit schema has no signature column. Verified reconcile_guard checks
    # the controller identity digest against this signature before atomically
    # recording the adoption. Keep this exact row stable across both snapshots;
    # do not fabricate an independent audit signature or rewrite the ledger.
    return None

def mac_worker_boundary(snapshot,roots,previous=None):
    processes=snapshot['processes'];descendants=set(roots);reasons=[]
    for pid in roots:
        current=processes.get(pid)
        if not current or current['state'].startswith('Z'):raise RuntimeError('Missing verified worker in fresh snapshot')
        if previous is not None and any(current.get(k)!=previous['processes'][pid].get(k) for k in ('ppid','pgid','started')):
            raise RuntimeError('Worker identity changed during local inspection')
    while True:
        found={p['pid'] for p in processes.values() if p['ppid'] in descendants}
        if found<=descendants:break
        descendants.update(found)
    groups={processes[pid]['pgid'] for pid in roots}
    for pid,p in processes.items():
        if pid in roots or p['state'].startswith('Z'):continue
        if pid in descendants:reasons.append(('worker-descendant:'+str(pid),'live adapter or command beneath worker'))
        elif p['pgid'] in groups:reasons.append(('worker-group:'+str(pid),'live process in worker service process group'))
    return reasons

def mac_known_process_reason(record,snapshot,independent=None):
    if not isinstance(record,dict):return 'invalid retained process evidence'
    result=record.get('result',record);evidence=result.get('evidence',{})
    identities=[record,result,evidence,result.get('reconciliation',{})]
    for container in (record,result,evidence,result.get('reconciliation',{})):
        if isinstance(container,dict) and 'process_identity' in container:identities.append(container['process_identity'])
    started=record.get('started_at');duration=evidence.get('duration_seconds');end=None
    if all(type(x) in (int,float) and math.isfinite(x) and x>=0 for x in (started,duration)):end=started+duration+5
    def matching(process):
        if process['state'].startswith('Z'):return False
        proof=(independent or {}).get(process['pid'])
        if (evidence.get('quiescent') is True and proof and proof.get('validated_dynamic_apple') is True
                and all(proof.get(k)==process.get(k) for k in ('pid','ppid','pgid','started'))
                and process['ppid']==1 and process['pgid']==process['pid']
                and sum(1 for p in snapshot['processes'].values() if p['pgid']==process['pgid'] and not p['state'].startswith('Z'))==1):return False
        born=process.get('started')
        return end is None or type(born) not in (int,float) or not math.isfinite(born) or born<=end
    for identity in identities:
        if not isinstance(identity,dict):return 'invalid retained process identity'
        for key in ('pid','pgid'):
            if key not in identity:continue
            number=identity[key]
            if type(number) is not int or number<=0:return 'invalid retained '+key+' identity'
            if key=='pid':live=[snapshot['processes'][number]] if number in snapshot['processes'] else []
            else:live=[p for p in snapshot['processes'].values() if p['pgid']==number]
            if any(matching(p) for p in live):return 'recorded execution '+key+' still live despite historical evidence'
    return None

def ledger_inventory(worker):
    inventory={}
    for table in ('actions','reconciliations','result_acknowledged'):
        if not worker.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
            inventory[table]=None;continue
        cursor=worker.execute('SELECT * FROM '+table+' ORDER BY id')
        inventory[table]=([x[0] for x in cursor.description],list(cursor))
    if 'signature' not in inventory['actions'][0]:raise RuntimeError('Action inventory lacks signatures')
    return inventory

def inspect_local(home,system,worker_roots,snapshot=None,restart_safety_mode=None,independent_process_proofs=None):
    blocked=[];proofs={};cgroup_clear=False;members=[];adopted={};known=[];history=[];evidence_fingerprint=None
    boundary=restart_safety_mode=='process-boundary'
    def block(op,reason):blocked.append({'operation':op,'reason':reason})
    def unresolved(op,reason):
        if boundary:history.append({'operation':op,'reason':reason})
        else:block(op,reason)
    try:
        if restart_safety_mode is not None and (not boundary or system!='Darwin'):raise RuntimeError('Unsupported restart safety mode')
        snap=snapshot if snapshot is not None else process_snapshot(system)
        roots=set(int(x) for x in worker_roots)
        if not roots or any(x not in snap['processes'] or snap['processes'][x]['state'].startswith('Z') for x in roots):raise RuntimeError('Missing verified worker process')
        descendants=set(roots)
        changed=True
        while changed:
            changed=False
            for p in snap['processes'].values():
                if p['ppid'] in descendants and p['pid'] not in descendants:descendants.add(p['pid']);changed=True
        for pid in sorted(descendants-roots):
            if not snap['processes'][pid]['state'].startswith('Z'):block('worker-descendant:'+str(pid),'live adapter or command beneath worker')
        cgroup_clear=False
        if system=='Linux':
            groups=set(snap['processes'][pid].get('service_cgroup') for pid in roots)
            if len(groups)==1 and None not in groups:
                group=next(iter(groups))
                if group not in ('','/') and group.endswith('/biomerte-terminal.service'):
                    members=[pid for pid,p in snap['processes'].items() if p.get('service_cgroup') and (p['service_cgroup']==group or p['service_cgroup'].startswith(group+'/')) and not p['state'].startswith('Z')]
                    cgroup_clear=set(members)==roots
                    for pid in sorted(set(members)-roots):block('worker-cgroup:'+str(pid),'live process in worker service cgroup')
        worker=readonly(home+'/worker.sqlite')
        try:
            if system=='Darwin':worker.execute('BEGIN')
            if boundary:ledger_before=ledger_inventory(worker)
            actions={op:(state,json.loads(value or '{}')) for op,state,value in worker.execute('SELECT id,state,result FROM actions')}
            audits=reconciled_rows(worker) if system=='Darwin' else {}
            acknowledged=set()
            if worker.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='result_acknowledged'").fetchone():
                acknowledged=set(row[0] for row in worker.execute('SELECT id FROM result_acknowledged'))
        finally:worker.close()
        if system=='Linux':
            db=readonly(home+'/terminal.sqlite')
            try:rows=list(db.execute('SELECT id,state,result FROM jobs'))
            finally:db.close()
            for op,state,value in rows:
                if state!='finished':proofs[op]='local supervisor state '+str(state);continue
                result=json.loads(value or '{}')
                if result.get('result_uploaded') is True and result.get('quiescent') is True and actions.get(op,('',))[0] in ('succeeded','failed'):
                    proofs[op]=None;continue
                identity=result.get('process_identity')
                # Old supervisor error receipts omit child PID. Preserve ledger state;
                # explicit completion plus an independently empty service cgroup proves
                # that restarting this service cannot kill a retained execution.
                if result.get('quiescent') is True and result.get('reason')=='supervisor_error' and isinstance(identity,dict) and identity.get('supervisor_pid') and not identity.get('pid') and cgroup_clear:
                    supervisor=snap['processes'].get(int(identity['supervisor_pid']))
                    if supervisor is None or supervisor['state'].startswith('Z'):
                        proofs[op]=None;continue
                proofs[op]=process_reason(identity,result,{},snap,system)
        else:
            root=home+'/terminal-results'
            def receipt_inventory():
                if boundary and not os.path.lexists(root):return (None,[])
                return mac_inventory(root)
            inventory=receipt_inventory();receipts={}
            for name,stamp in inventory[1]:
                directory=root+'/'+name
                if not stat.S_ISDIR(stamp[2]):continue
                path=directory+'/receipt.json'
                if not os.path.lexists(path):continue
                op,saved,stamp,reason=mac_receipt(directory)
                if reason:
                    proofs[op]=reason
                    if boundary:block(op,reason)
                    continue
                receipts[name]=(op,stamp)
                known.append((op,saved))
                state=saved.get('state');worker_state=actions.get(op,('',))[0]
                if state=='acknowledged' and worker_state in ('succeeded','failed'):proofs[op]=None;continue
                if state!='finished':proofs[op]='local receipt state '+str(state);continue
                evidence=saved.get('result',{}).get('evidence',{})
                proofs[op]=process_reason(evidence.get('process_identity'),evidence,saved,snap,system)
            # Releasing each lock bounds descriptor use. A second locked pass
            # must observe identical receipt bytes/inodes and an unchanged
            # directory inventory; any concurrent transition fails closed.
            if receipt_inventory()!=inventory:raise RuntimeError('Terminal receipt inventory changed during inspection')
            for name,(op,stamp) in sorted(receipts.items()):
                checked,_,current,reason=mac_receipt(root+'/'+name)
                if reason:
                    proofs[op]=reason
                    if boundary:block(op,reason)
                elif checked!=op or current!=stamp:
                    proofs[op]='receipt changed during inspection'
                    if boundary:block(op,proofs[op])
            if receipt_inventory()!=inventory:raise RuntimeError('Terminal receipt inventory changed during inspection')
        for op,reason in proofs.items():
            if reason:unresolved(op,reason)
        for op,(state,result) in actions.items():
            if op in proofs:continue
            # Acknowledged terminal outcomes already have the normal durable
            # completion proof. Their audit payloads may have been compacted;
            # still inspect every available process identity below.
            if state in ('succeeded','failed') and op in acknowledged:continue
            if system=='Darwin' and op in audits:
                reason=adopted_reason(audits[op])
                if reason:unresolved(op,reason)
                else:adopted[op]=audits[op]
                continue
            evidence=result.get('evidence',{}) if isinstance(result,dict) else {}
            if evidence.get('executed') is False and evidence.get('quiescent') is True:continue
            if evidence.get('quiescent') is True:
                reason=process_reason(evidence.get('process_identity'),evidence,{},snap,system)
                if reason:unresolved(op,reason)
            else:unresolved(op,'worker action lacks a local receipt or explicit quiescent proof')
        if system=='Darwin':
            receipt_context={op:record for op,record in known}
            known.extend((op,{'started_at':receipt_context[op].get('started_at'),'result':result} if op in receipt_context else result)
                         for op,(_,result) in actions.items())
            for op,row in audits.items():
                known.extend((op,json.loads(value)) for value in (row[4],row[6]))
            fresh=process_snapshot(system)
            for current in (snap,fresh):
                for op,reason in mac_worker_boundary(current,roots,snap):block(op,reason)
                for op,record in known:
                    allowed=(independent_process_proofs if boundary and actions.get(op,('',))[0] in ('succeeded','failed') and record.get('state') in (None,'finished','acknowledged') else None)
                    reason=mac_known_process_reason(record,current,allowed)
                    if reason:
                        if boundary and reason.startswith('invalid retained'):unresolved(op,reason)
                        else:block(op,reason)
            if adopted:
                worker=readonly(home+'/worker.sqlite')
                try:latest=reconciled_rows(worker)
                finally:worker.close()
                if any(latest.get(op)!=row for op,row in adopted.items()):raise RuntimeError('Reconciled action signature or audit changed during inspection')
            if boundary:
                worker=readonly(home+'/worker.sqlite')
                try:
                    worker.execute('BEGIN');ledger_after=ledger_inventory(worker)
                finally:worker.close()
                if ledger_after!=ledger_before:raise RuntimeError('Entire action/signature or audit inventory changed during inspection')
                # Recheck all retained receipt bytes/locks after the fresh process
                # snapshot, not merely the directory names observed beforehand.
                for name,(op,stamp) in sorted(receipts.items()):
                    checked,_,current,reason=mac_receipt(root+'/'+name)
                    if reason or checked!=op or current!=stamp:raise RuntimeError('Receipt or lock changed across restart proof')
                if receipt_inventory()!=inventory:raise RuntimeError('Receipt inventory changed across restart proof')
                evidence_fingerprint=hashlib.sha256(json.dumps([ledger_before,inventory,receipts,independent_process_proofs or {}],sort_keys=True,separators=(',',':')).encode()).hexdigest()
    except Exception as e:block('inspection',type(e).__name__+': '+str(e))
    report={'quiescent':not blocked and not history,'blocked':blocked,'checked_receipts':len(proofs),'worker_cgroup_clear':cgroup_clear,'worker_cgroup_members':members}
    if system=='Darwin':report['adopted_reconciliations']=sorted(adopted)
    if boundary:
        unique={(x['operation'],x['reason']) for x in history}
        report.update(restart_safety_mode=restart_safety_mode,restart_safe=not blocked,effects_quiescent=not blocked and not history,
                      unresolved_history=[{'operation':op,'reason':reason} for op,reason in sorted(unique)],evidence_fingerprint=evidence_fingerprint)
    return report
