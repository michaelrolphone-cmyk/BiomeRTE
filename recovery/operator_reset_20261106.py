import os, sys, json, time, signal, sqlite3, hashlib, plistlib, re, subprocess, traceback, fcntl
from pathlib import Path
from datetime import datetime, timezone
PURGE = 'reset-biome-idle-20261106-1820'
BASE = Path('/Users/micahelbyrns/.local/share')
USERHOME = BASE.parent.parent
AUDIT = BASE/'biomerte-maintenance'/'resets'/PURGE
AUDIT.mkdir(parents=True, exist_ok=True, mode=0o700)
os.umask(0o077)
PREVIOUS_REPORT={}
if (AUDIT/'report.json').is_file():
    PREVIOUS_REPORT=json.loads((AUDIT/'report.json').read_text())
    (AUDIT/('report-before-continuation-'+str(time.time_ns())+'.json')).write_text(json.dumps(PREVIOUS_REPORT,indent=2))
REPORT = {'id':PURGE,'started_at':datetime.now(timezone.utc).isoformat(),'services':[],'native':{},'errors':[]}
LOCKS = []
STOPPED = []
QUIESCENCE_ERRORS = []
JOB_WORKER = re.compile(r'(?:^|\s)-m\s+activation_field(?:\.(?:context_reconstruction|transforms))?\.jobs\s+worker(?:\s|$)')
def note(key,value):
    REPORT[key] = value
    (AUDIT/'report.json').write_text(json.dumps(REPORT,indent=2,default=str))
    print(json.dumps({key:value},default=str),flush=True)
def command(argv, timeout=20):
    try:
        p = subprocess.run(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=timeout)
        return {'exit':p.returncode,'stdout':p.stdout[-5000:],'stderr':p.stderr[-2000:]}
    except Exception as exc:
        return {'exit':-1,'error':type(exc).__name__+': '+str(exc)[:500]}
def processes():
    raw = subprocess.check_output(['/bin/ps','-axo','pid=,ppid=,uid=,lstart=,args='],text=True)
    rows={}
    for line in raw.splitlines():
        parts=line.strip().split(None,8)
        if len(parts)==9:
            rows[int(parts[0])]={'pid':int(parts[0]),'ppid':int(parts[1]),'uid':int(parts[2]),'birth':' '.join(parts[3:8]),'argv':parts[8]}
    return rows
def same(a,b):
    return b is not None and a['birth']==b['birth'] and a['argv']==b['argv']
def protected(rows):
    ids=set(); pid=os.getpid()
    while pid in rows and pid not in ids:
        ids.add(pid);pid=rows[pid]['ppid']
    return ids
def descendants(rows, seeds):
    ids=set(seeds)
    while True:
        extra={pid for pid,r in rows.items() if r['ppid'] in ids}
        if extra <= ids: return ids
        ids |= extra
def native_targets(rows, service_pids=()):
    safe=protected(rows)
    seeds=set(service_pids)
    for pid,r in rows.items():
        if pid in safe: continue
        argv=r['argv']
        if (str(BASE)+'/biome-' in argv or str(USERHOME)+'/Developer/biome-comfyui/' in argv or JOB_WORKER.search(argv)) and not any(x in argv for x in ('maintenance_agent.py','maintenance-agent.py','gateway-agent.py')):
            seeds.add(pid)
    # Admission records pin each holder to its process birth, so reused PIDs
    # cannot turn an unrelated process into a cancellation target.
    admission=BASE/'biome-model-runtime'/'admission.sqlite'
    if admission.is_file():
        db=sqlite3.connect(admission.as_uri()+'?mode=ro',uri=True,timeout=10)
        try:
            for pid,birth in db.execute('SELECT pid,birth FROM queue'):
                try: pid=int(pid)
                except (TypeError,ValueError): continue
                row=rows.get(pid)
                if row and pid not in safe and row['birth']==' '.join(str(birth).split()):
                    seeds.add(pid)
        finally: db.close()
    return {pid:r for pid,r in rows.items() if pid in descendants(rows,seeds) and pid not in safe}
def kill_exact(targets):
    outcome=[]
    for sig in (signal.SIGTERM,signal.SIGKILL):
        current=processes()
        for pid,old in targets.items():
            if same(old,current.get(pid)):
                try: os.kill(pid,sig);outcome.append({'pid':pid,'signal':int(sig)})
                except ProcessLookupError: pass
                except PermissionError as exc: REPORT['errors'].append({'pid':pid,'error':str(exc)})
        time.sleep(.8)
    current=processes()
    live=[pid for pid,old in targets.items() if same(old,current.get(pid))]
    return {'signals':outcome,'remaining':live}
def q(name):
    return '"'+name.replace('"','""')+'"'
def backup_db(path,db):
    destination=AUDIT/(hashlib.sha256(str(path).encode()).hexdigest()[:16]+'-'+path.name+'.sqlite')
    with sqlite3.connect(str(destination)) as out: db.backup(out)
    return str(destination)
def discover_services():
    found=[]
    for folder,domain in ((Path('/Library/LaunchDaemons'),'system'),(USERHOME/'Library/LaunchAgents','gui/501'),(Path('/Library/LaunchAgents'),'gui/501')):
        if not folder.exists(): continue
        for path in folder.glob('*.plist'):
            try:
                conf=plistlib.loads(path.read_bytes());label=conf.get('Label','')
            except Exception: continue
            if not label.startswith(('org.biomerte.','org.biome.')): continue
            if any(s in label for s in ('maintenance','gateway')) or ('controller' in label and 'watchdog' not in label): continue
            status=command(['/bin/launchctl','print',domain+'/'+label],10)
            if status['exit']!=0: continue
            m=re.search(r'^\s*pid = (\d+)',status.get('stdout',''),re.M)
            # launchctl output may exceed the log tail; read again only for a missing PID.
            if not m:
                p=subprocess.run(['/bin/launchctl','print',domain+'/'+label],capture_output=True,text=True,timeout=10)
                m=re.search(r'^\s*pid = (\d+)',p.stdout,re.M)
            found.append({'label':label,'domain':domain,'path':str(path),'pid':int(m.group(1)) if m else None,'working_directory':conf.get('WorkingDirectory',''),'resume':not any(s in label for s in ('corpus','resume','supervisor','assistant'))})
    return found
def stop_services(services):
    for svc in services:
        target=svc['domain']+'/'+svc['label']
        result=command(['/bin/launchctl','bootout',target],15)
        if result['exit']!=0 and svc['domain']=='system':
            result=command(['/usr/bin/sudo','-n','/bin/launchctl','bootout',target],15)
        svc['bootout']=result
        if result['exit']==0:
            STOPPED.append(svc)
        else:
            # A user-owned worker can still be stopped and its own single-worker lock held.
            root=Path(svc['working_directory'])
            if not (root/'worker.lock').exists():
                error={'service':target,'error':'Service stop failed','detail':result}
                REPORT['errors'].append(error);QUIESCENCE_ERRORS.append(error)
    REPORT['services']=services
def lock_worker_directories():
    results=[]
    for root in BASE.glob('biome-*'):
        path=root/'worker.lock'
        if not path.is_file(): continue
        lock=path.open('a')
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);LOCKS.append(lock);results.append(str(root))
        except OSError:
            lock.close();error={'worker':str(root),'error':'Worker lock still held'}
            REPORT['errors'].append(error);QUIESCENCE_ERRORS.append(error)
    return results
def purge_native_stores():
    roots=[BASE/n for n in ('biome-activation-field','biome-context-reconstruction','biome-field-transform','biome-model-runtime')]
    report={'databases':[],'cancelled_jobs':[],'cleared_admission':[],'unmatched_job_tables':[]}
    terminal={'succeeded','completed','partial','failed','cancelled','canceled','error','rejected'}
    seen=set()
    for root in roots:
        if not root.exists(): continue
        for directory,dirs,files in os.walk(root):
            dirs[:]=[d for d in dirs if d not in {'.git','node_modules','__pycache__','models','checkpoints','exports','artifacts','venv','.venv','source','corpus'} and not d.startswith('source-')]
            for name in files:
                if name.endswith(('-wal','-shm')): continue
                path=Path(directory)/name
                if not path.is_file(): continue
                try:
                    with path.open('rb') as f: header=f.read(16)
                except OSError: continue
                if header!=b'SQLite format 3\x00': continue
                resolved=str(path.resolve())
                if resolved in seen: continue
                seen.add(resolved)
                db=sqlite3.connect(str(path),timeout=15)
                try:
                    tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                    shapes={t:{r[1] for r in db.execute('PRAGMA table_info('+q(t)+')')} for t in tables}
                    jobs=[t for t,c in shapes.items() if {'id','version','state','action','progress','error','cancel'}<=c or (root.name=='biome-activation-field' and t=='jobs' and {'id','version','state','config','error'}<=c)]
                    queues=[t for t,c in shapes.items() if root.name=='biome-model-runtime' and {'ticket','id','pid','birth','kind','model','phase'}<=c]
                    if not jobs and not queues: continue
                    report['databases'].append({'path':str(path),'backup':backup_db(path,db),'tables':{t:sorted(shapes[t]) for t in jobs+queues}})
                    db.execute('BEGIN IMMEDIATE')
                    for table in jobs:
                        for job_id,version,state in db.execute('SELECT id,version,state FROM '+q(table)).fetchall():
                            if str(state).lower() in terminal: continue
                            error=json.dumps({'code':'cancelled','message':'Operator full execution purge','purge_id':PURGE,'previous_state':state})
                            cancellation=',cancel=1' if 'cancel' in shapes[table] else ''
                            db.execute('UPDATE '+q(table)+' SET state=?'+cancellation+',error=?,version=COALESCE(version,0)+1 WHERE id=?',('cancelled',error,job_id))
                            report['cancelled_jobs'].append({'id':job_id,'state':state,'version':version,'path':str(path)})
                    for table in queues:
                        rows=db.execute('SELECT ticket,id,pid,birth,kind,model,phase FROM '+q(table)).fetchall()
                        db.execute('DELETE FROM '+q(table))
                        report['cleared_admission'].append({'path':str(path),'table':table,'rows':rows})
                    db.commit()
                finally: db.close()
    return report
def controller():
    candidates=[]
    for folder in (USERHOME/'Library/LaunchAgents',Path('/Library/LaunchDaemons')):
        for path in folder.glob('*biomerte*controller*.plist'):
            try:
                conf=plistlib.loads(path.read_bytes())
                if conf.get('WorkingDirectory'): candidates.append(Path(conf['WorkingDirectory']))
                for arg in conf.get('ProgramArguments',[]):
                    if isinstance(arg,str) and arg.endswith('.py'): candidates.append(Path(arg).parent)
            except Exception: pass
    candidates.append(BASE/'biomerte-releases'/'assistant-20261104-r6')
    root=next((p for p in candidates if (p/'biomerte'/'app.py').is_file()),None)
    if root is None: raise RuntimeError('Installed controller source not found')
    sys.path.insert(0,str(root))
    from biomerte.app import Application
    app=Application(str(BASE/'biomerte-controller'))
    return app,str(root)

"""Journaled, resumable operator purge; no native execution or direct mirror writes.

Integrate these functions in place of purge_controller, then call
purge_controller_batched(store, realm, local_quiescent) outside store.execute.
PURGE and AUDIT come from the signed maintenance script. Each transaction has
one primary record, its required coupled writes, small audit references, and an
atomic completion receipt. Historical full values are private native snapshots.
"""

def _purge_kinds():
    # Disable controller producers before clearing their pending execution.
    return ('subscriptions', 'assistant_responsibilities',
            'assistant_start_outbox', 'assistant_wake_outbox',
            'assistant_learning_outbox', 'assistant_background',
            'assistant_episodes', 'assistant_assignment',
            'assistant_dispatch_failures', 'runs', 'waits', 'activations',
            'decisions', '_lease_queue', '_submission_active', 'actions',
            'leases', 'guards')


def _purge_eligible(kind, value):
    if value is None:
        return False
    if kind in ('_lease_queue', '_submission_active', 'assistant_wake_outbox',
                'assistant_assignment', 'assistant_dispatch_failures'):
        return True
    if kind == 'actions':
        return value.get('state') in ('queued', 'claimed', 'uncertain')
    if kind == 'leases':
        return value.get('state') != 'released'
    if kind == 'guards':
        return value.get('operator_purge') != PURGE or bool(value.get('holder'))
    if kind == 'runs' or kind == 'assistant_episodes':
        return value.get('state') == 'running'
    if kind == 'waits':
        return value.get('state') == 'open' or bool(value.get('claimant'))
    if kind in ('activations', 'decisions', 'assistant_start_outbox'):
        return value.get('state') == 'pending'
    if kind == 'assistant_learning_outbox':
        return not value.get('delivered')
    if kind == 'assistant_background':
        return value.get('state') == 'running' or not value.get('dispatch_reconciled')
    if kind == 'assistant_responsibilities':
        return (value.get('operator_purge') != PURGE or value.get('desired') != 'pause'
                or value.get('active_cycle') is not None or value.get('cycle_permit')
                or bool(value.get('pending_triggers')) or value.get('next_inspection_at') is not None)
    if kind == 'subscriptions':
        return bool(value.get('process'))
    return False


def _purge_execute(store, callback):
    import time
    for attempt in range(40):
        try:
            return store.execute(callback)
        except Exception as error:
            message = str(error).lower()
            conflict = (getattr(error, 'status', None) == 409 and
                        (type(error).__name__ == 'JournalConflict' or
                         (any(word in message for word in ('journal', 'shared', 'mirror')) and
                          any(word in message for word in ('conflict', 'advanced', 'changed', 'concurrent', 'retry')))))
            if (not conflict or attempt == 39 or
                    any(word in message for word in ('rolled back', 'rollback', 'history mismatch', 'discontinuity'))):
                raise
            time.sleep(min(0.02 * (attempt + 1), 0.5))


def _purge_before_ref(realm, kind, key, value):
    import os
    from pathlib import Path
    from biomerte.store import digest, encode
    payload = encode(value).encode('utf-8')
    value_hash = digest(value)
    folder = Path(AUDIT) / 'controller-before' / digest(realm)[:20]
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder / (digest([PURGE, kind, key]) + '-' + value_hash + '.json')
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.read_bytes() != payload:
            raise RuntimeError('Operator purge snapshot hash/path mismatch')
    else:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    return {'purge_id': PURGE, 'realm': realm, 'kind': kind, 'key': key,
            'sha256': value_hash, 'bytes': len(payload), 'path': str(path)}


def purge_controller(tx, realm, local_quiescent, kind, key, before_refs):
    """One atomic unit; no I/O, safe to re-evaluate after journal CAS conflict."""
    import copy
    from biomerte.store import digest
    from biomerte import process
    unit_id = digest([PURGE, realm, kind, key])
    receipt = tx.get('_operator_purge_units', unit_id)
    if receipt is not None:
        return receipt
    old = tx.get(kind, key)
    if not _purge_eligible(kind, old):
        return {'skipped': True, 'counts': {}}
    counts = {}

    def archive(record_kind, record_key, value):
        ref = before_refs.get((record_kind, record_key))
        if ref is None or ref['sha256'] != digest(value):
            raise RuntimeError('OPERATOR_PURGE_SNAPSHOT_CHANGED')
        archive_id = digest([PURGE, realm, record_kind, record_key])
        if tx.get('_operator_purge_before', archive_id) is None:
            tx.put('_operator_purge_before', archive_id, ref)

    def save(record_kind, record_key, previous, updated):
        if previous != updated:
            archive(record_kind, record_key, previous)
            tx.put(record_kind, record_key, updated)
            counts[record_kind] = counts.get(record_kind, 0) + 1

    def drop(record_kind, record_key, previous):
        archive(record_kind, record_key, previous)
        tx.drop(record_kind, record_key)
        counts[record_kind] = counts.get(record_kind, 0) + 1

    outcome = {'cancelled': True, 'reason': 'Operator revoked all existing work and claims',
               'operator_purge': PURGE}
    new = copy.deepcopy(old)
    if kind == 'actions':
        is_local = '.mac.' in old.get('resource', '') or old.get('provider') in ('builtin', 'routine')
        result = dict(outcome, previous_state=old['state'], previous_result=old.get('result'),
                      reconciliation={'quiescent': bool(is_local and local_quiescent),
                                      'authority': 'operator maintenance',
                                      'historical_effects': 'Retained as observed; not undone',
                                      'purge_id': PURGE})
        new.update(state='failed', result=result, operator_purge=PURGE)
        save(kind, key, old, new)
        tx.emit('operation.failed', {'id': key, 'operator_purge': PURGE})
    elif kind == '_submission_active':
        sub = tx.get('_submissions', key)
        if sub is not None:
            save('_submissions', key, sub, dict(sub, cancelled=True, operator_purge=PURGE))
        drop(kind, key, old)
    elif kind in ('_lease_queue', 'assistant_wake_outbox', 'assistant_assignment', 'assistant_dispatch_failures'):
        drop(kind, key, old)
    elif kind == 'leases':
        new.update(state='released', operator_purge=PURGE)
        new.pop('queue_key', None)
        save(kind, key, old, new)
        tx.emit('lease.released', {'lease': key, 'operator_purge': PURGE})
    elif kind == 'guards':
        new.update(holder='', generation=old.get('generation', 0) + 1, operator_purge=PURGE)
        save(kind, key, old, new)
    elif kind in ('runs', 'assistant_episodes', 'assistant_background'):
        if old.get('state') == 'running':
            new.update(state='cancelled', result=copy.deepcopy(outcome), operator_purge=PURGE)
            if kind == 'runs':
                new.update(tokens=[], joins={})
        if kind == 'assistant_background':
            new['dispatch_reconciled'] = True
        save(kind, key, old, new)
    elif kind == 'waits':
        new['operator_purge'] = PURGE
        if old.get('state') == 'open':
            new.update(state='failed', error='Operator full execution purge')
        new.pop('claimant', None)
        save(kind, key, old, new)
    elif kind == 'activations':
        new.update(state='failed', evidence=copy.deepcopy(outcome), operator_purge=PURGE)
        save(kind, key, old, new)
    elif kind == 'decisions':
        new.update(state='withdrawn', operator_purge=PURGE)
        save(kind, key, old, new)
    elif kind == 'assistant_start_outbox':
        new.update(state='cancelled', operator_purge=PURGE)
        save(kind, key, old, new)
    elif kind == 'assistant_learning_outbox':
        new.update(delivered=True, delivery_disposition='cancelled', operator_purge=PURGE)
        save(kind, key, old, new)
    elif kind == 'assistant_responsibilities':
        new.update(desired='pause', active_cycle=None, cycle_permit=False,
                   pending_triggers=[], next_inspection_at=None,
                   coverage={'kind': 'pause', 'events': ['operator_resume']},
                   revision=old['revision'] + 1, epoch=old['epoch'] + 1,
                   operator_purge=PURGE)
        save(kind, key, old, new)
        if tx.get('definitions', 'assistant.supervision'):
            sid = digest([new['id'], new['revision'], new['epoch']])
            if not tx.get('assistant_supervision_receipts', sid):
                run = process.new_run(tx, 'assistant.supervision', {'frame': {
                    'responsibility_id': new['id'], 'owner_epoch': new['epoch'], 'goal': new['goal']}})
                run.update(state='cancelled', tokens=[], joins={},
                           result=copy.deepcopy(outcome), operator_purge=PURGE)
                tx.put('runs', run['id'], run)
                tx.put('assistant_supervision_receipts', sid,
                       {'id': sid, 'run_id': run['id'], 'operator_purge': PURGE})
    elif kind == 'subscriptions':
        new.update(maintenance_previous_process=old['process'], process='',
                   cursor=tx.get('_meta', 'events', 0), operator_purge=PURGE)
        save(kind, key, old, new)
    else:
        raise RuntimeError('Unsupported operator purge kind: ' + kind)
    receipt = {'purge_id': PURGE, 'realm': realm, 'kind': kind, 'key': key, 'counts': counts}
    tx.put('_operator_purge_units', unit_id, receipt)
    return receipt


def purge_controller_batched(store, realm, local_quiescent):
    import json
    from datetime import datetime, timezone
    from biomerte.store import digest

    def pending(tx, kind):
        return [(key, json.loads(raw)) for key, raw in tx.db.execute(
            'SELECT id,value FROM records WHERE kind=? ORDER BY id', (kind,)).fetchall()
            if _purge_eligible(kind, json.loads(raw))]

    marker = _purge_execute(store, lambda tx: tx.get('_operator_purges', PURGE))
    if marker is not None:
        return marker
    # A few sweeps allow controller work already in flight at the pause boundary
    # to finish entering the journal. Existing completed units are never repeated.
    for sweep in range(4):
        for kind in _purge_kinds():
            rows = _purge_execute(store, lambda tx, k=kind: pending(tx, k))
            for key, unused in rows:
                for attempt in range(12):
                    def read_unit(tx):
                        unit_id = digest([PURGE, realm, kind, key])
                        if tx.get('_operator_purge_units', unit_id) is not None:
                            if _purge_eligible(kind, tx.get(kind, key)):
                                raise RuntimeError('Execution reappeared after purge: ' + kind + '/' + key)
                            return None
                        values = {(kind, key): tx.get(kind, key)}
                        if kind == '_submission_active':
                            values[('_submissions', key)] = tx.get('_submissions', key)
                        return values
                    values = _purge_execute(store, read_unit)
                    if values is None or not _purge_eligible(kind, values.get((kind, key))):
                        break
                    refs = {(k, i): _purge_before_ref(realm, k, i, value)
                            for (k, i), value in values.items() if value is not None}
                    try:
                        _purge_execute(store, lambda tx: purge_controller(
                            tx, realm, local_quiescent, kind, key, refs))
                        break
                    except RuntimeError as error:
                        if str(error) != 'OPERATOR_PURGE_SNAPSHOT_CHANGED' or attempt == 11:
                            raise
        def finish(tx):
            existing = tx.get('_operator_purges', PURGE)
            if existing is not None:
                return existing
            remaining = {kind: len(pending(tx, kind)) for kind in _purge_kinds()}
            remaining = {kind: count for kind, count in remaining.items() if count}
            if remaining:
                return {'incomplete': remaining}
            counts = {}
            for raw, in tx.db.execute("SELECT value FROM records WHERE kind='_operator_purge_units'").fetchall():
                receipt = json.loads(raw)
                if receipt.get('purge_id') == PURGE and receipt.get('realm') == realm:
                    for kind, count in receipt.get('counts', {}).items():
                        counts[kind] = counts.get(kind, 0) + count
            result = {'id': PURGE, 'realm': realm, 'counts': counts,
                      'native_mac_quiescent': local_quiescent,
                      'remote_native_quiescence': 'unverified',
                      'journal_mode': 'one_primary_record_per_transaction',
                      'completed_at': datetime.now(timezone.utc).isoformat()}
            tx.emit('maintenance.execution.purged', result)
            tx.put('_operator_purges', PURGE, result)
            return result
        result = _purge_execute(store, finish)
        if 'incomplete' not in result:
            return result
    raise RuntimeError('Controller remained active after bounded purge sweeps: ' + json.dumps(result))


def purge_worker_ledgers(app):
    """Settle stopped workers only within their pinned controller authority."""
    from biomerte.store import digest

    result = []
    snapshots = {}

    def snapshot(realm):
        if realm not in snapshots:
            def read(tx):
                return {
                    'actions': {a['id']: a for a in tx.rows('actions')},
                    'guards': {key: json.loads(raw) for key, raw in tx.db.execute(
                        "SELECT id,value FROM records WHERE kind='guards'").fetchall()},
                }
            snapshots[realm] = app.stores[realm].execute(read)
        return snapshots[realm]

    for root in sorted(BASE.glob('biome-*')):
        path = root / 'worker.sqlite'
        if not path.is_file():
            continue
        db = sqlite3.connect(str(path), timeout=15)
        try:
            tables = {r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'actions', 'fences'} <= tables:
                continue
            columns = {r[1] for r in db.execute('PRAGMA table_info(actions)')}
            if not {'id', 'signature', 'state', 'result', 'resource'} <= columns:
                continue

            # Worker.__init__ stores encode([remote.site, remote.realm,
            # remote.node]) at authority(id=1). Never combine realm counters.
            if 'authority' not in tables:
                raise RuntimeError('Worker authority is missing: ' + str(path))
            authority_row = db.execute(
                'SELECT value FROM authority WHERE id=1').fetchone()
            authority = json.loads(authority_row[0]) if authority_row else None
            if (not isinstance(authority, list) or len(authority) != 3 or
                    any(not isinstance(v, str) or not v for v in authority)):
                raise RuntimeError('Invalid worker authority: ' + str(path))
            site, realm, provider = authority
            if site != app.site or realm not in app.stores:
                raise RuntimeError('Foreign worker authority: ' + str(path))
            current = snapshot(realm)

            rows = db.execute(
                "SELECT id,signature,state,result,resource FROM actions "
                "WHERE state IN ('queued','claimed','uncertain')").fetchall()
            updates = []
            orphan_ids = []
            for op, signature, state, previous, guard in rows:
                action = current['actions'].get(op)
                if action is None:
                    # The operator revoked all local execution. A missing
                    # controller record is an explicitly local cancellation,
                    # never an adopted result from a different realm/provider.
                    outcome = {
                        'cancelled': True, 'operator_purge': PURGE,
                        'previous_state': state,
                        'previous_result': json.loads(previous) if previous else None,
                        'controller_record': 'absent in pinned realm',
                        'worker_authority': {'site': site, 'realm': realm,
                                             'provider': provider},
                    }
                    settled = 'failed'
                    orphan_ids.append(op)
                else:
                    if action.get('provider') != provider:
                        raise RuntimeError('Worker provider mismatch: ' + op +
                                           ' in ' + str(path))
                    expected = digest([
                        action['contract'], action['inputs'], action['resource'],
                        action['generation'], action['lease'],
                    ])
                    if (signature != expected or
                            guard != action.get('guard', action.get('resource'))):
                        raise RuntimeError('Worker operation binding mismatch: ' +
                                           op + ' in ' + str(path))
                    settled = action.get('state')
                    outcome = action.get('result')
                    if settled not in ('succeeded', 'failed') or not isinstance(outcome, dict):
                        raise RuntimeError('Controller operation remains unsettled: ' + op)
                    # Preserve an already completed exact result. Never turn a
                    # historical controller success into a local failed result.
                updates.append((op, signature, state, previous, settled, outcome))

            fence_updates = []
            for guard, generation in db.execute(
                    'SELECT resource,gen FROM fences').fetchall():
                remote_guard = current['guards'].get(guard)
                target_generation = generation
                if remote_guard is not None:
                    if remote_guard.get('holder'):
                        raise RuntimeError('Controller guard is still held: ' + guard)
                    remote_generation = remote_guard['generation']
                    if generation > remote_generation:
                        raise RuntimeError('Native fence is ahead of its pinned realm: ' + guard)
                    target_generation = remote_generation
                fence_updates.append((target_generation, guard))

            # All bindings are checked before this database is changed.
            backup = backup_db(path, db)
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS operator_purge_actions('
                       'purge_id TEXT,id TEXT,previous_state TEXT,previous_result TEXT,'
                       'PRIMARY KEY(purge_id,id))')
            for op, signature, state, previous, settled, outcome in updates:
                db.execute('INSERT OR IGNORE INTO operator_purge_actions VALUES(?,?,?,?)',
                           (PURGE, op, state, previous))
                changed = db.execute(
                    'UPDATE actions SET state=?,result=? '
                    'WHERE id=? AND signature=? AND state=?',
                    (settled, json.dumps(outcome), op, signature, state)).rowcount
                if changed != 1:
                    raise RuntimeError('Worker action changed during reset: ' + op)
            for generation, guard in fence_updates:
                db.execute('UPDATE fences SET holder=?,gen=? WHERE resource=?',
                           ('', generation, guard))
            db.commit()
            result.append({
                'path': str(path), 'realm': realm, 'provider': provider,
                'settled': len(updates),
                'cancelled': sum(row[4] == 'failed' for row in updates),
                'completed_results_adopted': sum(row[4] == 'succeeded' for row in updates),
                'local_orphans_cancelled': orphan_ids, 'backup': backup,
            })
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    # LM Studio delegation cache is independent of provider fence journals.
    path = BASE / 'biome-lmstudio' / 'agent.sqlite'
    if path.is_file():
        db = sqlite3.connect(str(path), timeout=15)
        try:
            tables = {r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if 'work' in tables:
                backup = backup_db(path, db)
                ids = [r[0] for r in db.execute('SELECT id FROM work WHERE outcome IS NULL')]
                db.execute('UPDATE work SET outcome=? WHERE outcome IS NULL', (
                    json.dumps({'error': 'Operator full execution purge',
                                'operator_purge': PURGE}),))
                db.commit()
                result.append({'path': str(path), 'cancelled': len(ids), 'backup': backup})
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
    return result

def verify_controller(app):
    answer={}
    for realm,store in app.stores.items():
        def read(tx):
            return {'leases':[r['id'] for r in tx.rows('leases') if r.get('state')!='released'],
                    'actions':[r['id'] for r in tx.rows('actions') if r.get('state') in ('queued','claimed','uncertain')],
                    'runs':[r['id'] for r in tx.rows('runs') if r.get('state')=='running'],
                    'waits':[r['id'] for r in tx.rows('waits') if r.get('state')=='open' or r.get('claimant')],
                    'lease_queue':len(tx.rows('_lease_queue')),'submission_queue':len(tx.rows('_submission_active')),
                    'held_guards':sum(bool(r.get('holder')) for r in tx.rows('guards'))}
        answer[realm]=store.execute(read)
    return answer

def pause_resume_guards():
    result=[]
    names={'lmstudio-corpus-resume-20261106.json','subject-corpus-01-fit-guard.json'}
    for root in (BASE/'biome-lmstudio',BASE/'biome-activation-field',BASE/'biome-model-runtime'):
        if not root.exists(): continue
        for directory,dirs,files in os.walk(root):
            dirs[:]=[d for d in dirs if d not in {'.git','node_modules','__pycache__','models','checkpoints','exports','artifacts','venv','.venv','source'} and not d.startswith('source-')]
            for name in names.intersection(files):
                path=Path(directory)/name
                try:
                    old=json.loads(path.read_text())
                    (AUDIT/(root.name+'-'+name)).write_text(json.dumps(old,indent=2))
                    if isinstance(old,dict):
                        new=dict(old,state='cancelled',cancelled=True,disabled=True,resume=False,operator_purge=PURGE)
                        path.write_text(json.dumps(new,indent=2))
                        result.append(str(path))
                except Exception as exc: REPORT['errors'].append({'path':str(path),'error':str(exc)[:300]})
    return result
def restart_services():
    for lock in LOCKS:
        fcntl.flock(lock,fcntl.LOCK_UN);lock.close()
    LOCKS.clear()
    results=[]
    for svc in STOPPED:
        if not svc['resume']:
            results.append({'service':svc['label'],'state':'paused','reason':'Autonomous producer or restart guard'})
            continue
        r=command(['/bin/launchctl','bootstrap',svc['domain'],svc['path']],20)
        if r['exit']!=0 and svc['domain']=='system':
            r=command(['/usr/bin/sudo','-n','/bin/launchctl','bootstrap',svc['domain'],svc['path']],20)
        results.append({'service':svc['label'],'bootstrap':r})
        if r['exit']!=0:
            REPORT['errors'].append({'service':svc['label'],'error':'Provider restart failed','detail':r})
    # Services whose privileged bootout was unavailable were held by their worker lock.
    # They restart through their existing KeepAlive setting after this lock is released.
    return results
def main():
    app,source=controller()
    note('controller_source',source)
    services=discover_services()
    loaded={(s['domain'],s['label']) for s in services}
    for svc in PREVIOUS_REPORT.get('services',[]):
        if svc.get('bootout',{}).get('exit')==0 and (svc['domain'],svc['label']) not in loaded:
            STOPPED.append(svc)
    before=processes()
    targets=native_targets(before,[s['pid'] for s in services if s['pid']])
    (AUDIT/'native-before.json').write_text(json.dumps(list(targets.values()),indent=2))
    note('native_target_count',len(targets))
    stop_services(services)
    REPORT['services']=services+[s for s in STOPPED if (s['domain'],s['label']) not in loaded]
    note('native_termination',kill_exact(targets))
    # Catch job children started between the initial census and service shutdown.
    remaining_targets=native_targets(processes())
    if remaining_targets:
        note('native_termination_second_pass',kill_exact(remaining_targets))
    note('worker_locks_held',lock_worker_directories())
    lms=USERHOME/'.lmstudio/bin/lms'
    if lms.is_file():
        for label,args in (('lmstudio_stop',['server','stop']),('lmstudio_unload',['unload','--all'])):
            r=command([str(lms)]+args,25);note(label,r)
            if r['exit']!=0:
                error={'service':label,'error':'LM Studio quiescence command failed','detail':r}
                REPORT['errors'].append(error);QUIESCENCE_ERRORS.append(error)
    note('resume_guards_paused',pause_resume_guards())
    alive=native_targets(processes())
    # KeepAlive may have launched a worker that immediately exits on our lock.
    if alive:
        kill_exact(alive);alive=native_targets(processes())
    local_quiescent=not alive and not QUIESCENCE_ERRORS and not any('pid' in e and 'error' in e for e in REPORT['errors'])
    note('native_quiescence',{'confirmed':local_quiescent,'remaining_pids':sorted(alive)})
    realm_results={}
    for realm,store in app.stores.items():
        realm_results[realm]=purge_controller_batched(store,realm,local_quiescent)
    note('controller_purge',realm_results)
    note('controller_before_restart',verify_controller(app))
    if not local_quiescent:
        raise RuntimeError('Controller claims cleared, but native quiescence is incomplete; native database rewrite and provider restart withheld; see errors')
    note('native_stores',purge_native_stores())
    note('worker_ledgers',purge_worker_ledgers(app))
    restart_started=time.time()
    note('service_restart',restart_services())
    if lms.is_file():
        r=command([str(lms),'server','start'],25);note('lmstudio_start',r)
        if r['exit']!=0: REPORT['errors'].append({'service':'lmstudio','error':'Model server restart failed','detail':r})
    time.sleep(12)
    note('controller_after_restart',verify_controller(app))
    health={}
    for root in BASE.glob('biome-*'):
        for name in ('health.json','worker-health.json'):
            path=root/name
            if path.is_file():
                try:
                    raw=json.loads(path.read_text())
                    health[str(path)]={k:raw[k] for k in ('state','last_poll','observed_at','pending','blocked','provider','resource') if k in raw}
                    health[str(path)]['fresh_after_restart']=max(float(raw.get('last_poll',0) or 0),float(raw.get('observed_at',0) or 0))>=restart_started
                except Exception: pass
    note('provider_health',health)
    after=processes()
    survivors=[pid for pid,old in targets.items() if same(old,after.get(pid))]
    jobs=[{'pid':pid,'argv':r['argv']} for pid,r in after.items() if JOB_WORKER.search(r['argv']) and pid not in protected(after)]
    note('final_native_jobs',{'old_process_survivors':survivors,'job_workers':jobs})
    report=verify_controller(app)
    clear=all(not v for realm in report.values() for v in realm.values())
    ok=clear and not survivors and not jobs and not REPORT['errors']
    note('outcome',{'controller_all_claims_cleared':clear,'native_mac_old_jobs_stopped':not survivors and not jobs,'mac_reset_checks_passed':ok,'remote_native_quiescence':'unverified','provider_readiness':'See fresh health and independent capability probes','finished_at':datetime.now(timezone.utc).isoformat(),'audit_path':str(AUDIT)})
    if not clear: raise RuntimeError('Controller execution records appeared after reset; see report')
    if not ok: raise RuntimeError('Reset has unresolved native or restart errors; see report')
try:
    main()
except BaseException as exc:
    REPORT['fatal']=type(exc).__name__+': '+str(exc)
    (AUDIT/'error.txt').write_text(traceback.format_exc())
    note('fatal',REPORT['fatal'])
    raise

