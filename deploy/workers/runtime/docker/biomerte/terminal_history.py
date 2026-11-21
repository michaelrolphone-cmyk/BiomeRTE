"""Historical terminal diagnostic evidence checks. No workspace enumeration."""
import hashlib
import json
import os
from .store import digest, encode, Fault


def authority(remote, operation, known=None, allow_reconciled=False):
    action = remote.call('cap.status', {'operation':operation, 'recovery':True})
    if not isinstance(action,dict) or action.get('id')!=operation or action.get('provider')!=remote.node or action.get('state') not in ('succeeded','failed'):
        raise Fault('Historical terminal authority identity differs',409)
    result=action.get('result')
    if known is not None:
        try:
            retained=json.loads(known['result'])
            identity=action.get('identity_digest') if action.get('result_expired') is True else digest([action[k] for k in ('contract','inputs','resource','generation','lease')])
            result_hash=action.get('result_digest') if action.get('result_expired') is True else digest(result)
            if known['id']!=operation or known['state']!=action['state'] or known['signature']!=identity or digest(retained)!=result_hash:
                raise ValueError('Retained worker identity/result differs')
        except (KeyError,ValueError,TypeError):raise Fault('Historical retained authority proof differs',409)
        result=retained
    from .worker_history import quiescent
    if not isinstance(result,dict) or not (result.get('evidence',{}).get('quiescent') is True or (allow_reconciled and known is not None and quiescent(result))):
        raise Fault('Historical terminal authority is not complete and quiescent',409)
    return dict(action,result=result)


def streams(result, paths):
    for name,path in paths.items():
        if os.path.islink(path) or os.path.realpath(path) != os.path.abspath(path):
            raise Fault('Unsafe historical diagnostic path',409)
        h=hashlib.sha256();size=0
        with open(path,'rb') as f:
            while True:
                block=f.read(65536)
                if not block:break
                size+=len(block);h.update(block)
        expected=result.get('streams',{}).get(name,{})
        if expected.get('sha256') != h.hexdigest() or expected.get('size') != size:
            raise Fault('Historical diagnostic differs from acknowledged stream',409)


def mac_plan(adapter, cfg, remote, operation, known=None):
    directory=adapter.ROOT/'terminal-results'/hashlib.sha256(operation.encode()).hexdigest()
    if directory.is_symlink() or os.path.realpath(str(directory)) != str(directory.absolute()):
        raise Fault('Unsafe historical terminal directory',409)
    if any((directory/name).is_symlink() for name in ('receipt.json','published.json','lock')):
        raise Fault('Unsafe historical terminal metadata path',409)
    if not os.path.lexists(str(directory)):
        # No adapter payload exists to enroll or delete. Require the full exact
        # retained worker proof and fresh authoritative quiescence nevertheless.
        if known is None:raise Fault('Missing directory requires retained worker authority',409)
        action=authority(remote,operation,known,allow_reconciled=True)
        return {'operation':operation,'absent_directory':str(directory),
                'identity':known['signature'],'result_digest':digest(action['result'])}
    saved=json.loads((directory/'receipt.json').read_text())
    if saved.get('retention_version') == 1:
        return {'already_enrolled':True,'operation':operation}
    action=authority(remote,operation,known);result=action['result']
    signature=saved.get('signature') if known is not None else digest([operation,action['inputs'],cfg['terminal_profile']])
    if not isinstance(signature,str) or len(signature)!=64 or any(c not in '0123456789abcdef' for c in signature):
        raise Fault('Historical terminal receipt signature malformed',409)
    if saved.get('operation') != operation or saved.get('signature') != signature or saved.get('state') != 'finished':
        raise Fault('Historical terminal receipt identity mismatch',409)
    if any(result.get(k) != v for k,v in saved.get('result',{}).items()) or not saved.get('result'):
        raise Fault('Historical terminal receipt result mismatch',409)
    paths={name:str(directory/name) for name in ('stdout','stderr')}
    streams(result,paths)
    return {'operation':operation,'receipt':saved,'result':result,'identity':known['signature'] if known is not None else digest([action[k] for k in ('contract','inputs','resource','generation','lease')])}


def mac_apply(adapter,cfg,remote,manifest,known=None):
    import fcntl
    operation=manifest['operation']
    directory=adapter.ROOT/'terminal-results'/hashlib.sha256(operation.encode()).hexdigest()
    if 'absent_directory' in manifest:
        current=mac_plan(adapter,cfg,remote,operation,known)
        if current!=manifest:raise Fault('Absent historical adapter evidence changed',409)
        return {'no_adapter_payload':True,'operation':operation,'result_digest':current['result_digest']}
    if directory.is_symlink() or (directory/'lock').is_symlink():
        raise Fault('Unsafe historical terminal lock',409)
    with (directory/'lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        current=mac_plan(adapter,cfg,remote,operation,known)
        if current.get('already_enrolled'):
            return current
        if current != manifest:
            raise Fault('Historical terminal preimage changed',409)
        saved=dict(current['receipt']);saved['retention_version']=1
        # Persist the exact acknowledged upload before making the receipt eligible.
        adapter.save_receipt(directory/'published.json',current['result'])
        adapter.save_receipt(directory/'receipt.json',saved)
    return {'enrolled':operation,'result_digest':digest(current['result'])}


def pi_plan(db,home,remote,operation,known=None):
    if db.execute('SELECT 1 FROM terminal_retention_eligible WHERE id=?',(operation,)).fetchone():
        return {'already_enrolled':True,'operation':operation}
    row=db.execute('SELECT rowid,body,state,result FROM jobs WHERE id=?',(operation,)).fetchone()
    if row is None or row[2] != 'finished':
        raise Fault('Historical Pi job is unresolved',409)
    body=json.loads(row[1]);local=json.loads(row[3]);action=authority(remote,operation,known);result=action['result']
    inputs=action.get('inputs')
    if action.get('result_expired') is True:
        inputs={'argv':body.get('argv')}
        try:identity=digest([action['contract'],inputs,action['resource'],action['generation'],action['lease']])
        except KeyError:raise Fault('Historical Pi request identity unavailable',409)
        if identity!=action.get('identity_digest'):raise Fault('Historical Pi request preimage differs',409)
    if not isinstance(inputs,dict):raise Fault('Historical Pi request unavailable',409)
    work=os.path.join(os.path.abspath(home),'jobs',hashlib.sha256(operation.encode()).hexdigest())
    if body.get('workspace') != work or body.get('argv') != inputs.get('argv') or local.get('quiescent') is not True or local.get('reason') == 'supervisor_error':
        raise Fault('Historical Pi job identity or quiescence mismatch',409)
    for key in ('exit_code','duration_seconds','reason','process_identity','quiescent'):
        if result.get('evidence',{}).get(key) != local.get(key):
            raise Fault('Historical Pi supervisor evidence mismatch',409)
    paths={name:os.path.join(work,name+'.log') for name in ('stdout','stderr')}
    if any(local.get(name) != path for name,path in paths.items()):
        raise Fault('Historical Pi diagnostic path mismatch',409)
    streams(result,paths)
    return {'operation':operation,'row':list(row),'result':result,'paths':paths}


def pi_apply(db,home,remote,manifest,known=None):
    operation=manifest['operation']
    # Obtain authenticated evidence before taking a local write lock.
    current=pi_plan(db,home,remote,operation,known)
    if current.get('already_enrolled'):return current
    if current != manifest:raise Fault('Historical Pi manifest changed',409)
    with db:
        row=db.execute('SELECT rowid,body,state,result FROM jobs WHERE id=?',(operation,)).fetchone()
        if list(row) != manifest['row']:raise Fault('Historical Pi preimage changed',409)
        value=encode(current['result']);paths=current['paths']
        # Pi count/byte policy uses this ordering field, not an age cutoff.
        # Negative original row order is strictly older than new epoch values.
        db.execute('INSERT INTO terminal_uploads VALUES(?,?,?,?,?,?,0)',(operation,value,digest(current['result']),paths['stdout'],paths['stderr'],row[0]-1000000000000))
        db.execute('INSERT INTO terminal_retention_eligible VALUES(?)',(operation,))
    return {'enrolled':operation,'result_digest':digest(current['result'])}
