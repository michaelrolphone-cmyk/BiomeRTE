"""Bounded whole-record expiry, without replay or invented acknowledgements."""
import json,subprocess,os,tempfile,hashlib
from .store import Fault,encode

class AdapterExpiryNonzero(Fault):
    """Adapter process exited nonzero; partial effects are not ruled out."""
    def __init__(self,returncode):
        self.returncode=returncode
        Fault.__init__(self,"Adapter expiry exited nonzero; completed row retained",503)

TABLES=('result_cleanup','result_recovery_pending','result_retention_eligible',
        'historical_result_enrolled','reconciliation_retention_eligible',
        'reconciliation_debug','reconciliations','result_debug','result_acknowledged','actions')

def _row(db,operation):
    return db.execute('SELECT id,signature,state,result,resource FROM actions WHERE id=?',(operation,)).fetchone()

def _adapter(worker,row,historical=None):
    operation,signature,state,body,guard=row
    offers=[o for o in worker.offers if guard==o['resource'] or guard.startswith('!slot:'+o['resource']+':')]
    if not offers and len(worker.offers)==1:offers=worker.offers
    if len(offers)>1 or (worker.offers and not offers):raise Fault('Ambiguous adapter expiry route',409)
    argv=offers[0].get('acknowledge_argv') if offers else None
    if not argv:return
    if not isinstance(argv,list) or not argv or not all(isinstance(a,str) for a in argv) or not os.path.isabs(argv[0]):raise Fault('Invalid adapter expiry argv')
    payload={'operation':operation,'expire':True}
    if historical is not None:payload['historical_result']=historical
    with tempfile.TemporaryFile() as output:
        proc=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=output,stderr=subprocess.DEVNULL)
        try:proc.communicate(encode(payload).encode(),timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill();proc.communicate();raise Fault('Adapter expiry incomplete; reconcile before retry',503)
        if proc.returncode:raise AdapterExpiryNonzero(proc.returncode)
        output.seek(0);raw=output.read(4097)
        if len(raw)>4096:raise Fault('Adapter expiry receipt exceeds bound',503)
        try:confirmed=json.loads(raw.decode('utf-8'))
        except (ValueError,UnicodeError):raise Fault('Adapter expiry receipt missing',503)
        if not isinstance(confirmed,dict) or (confirmed.get('expired') is not True and confirmed.get('acknowledged_adapter_window') is not True):raise Fault('Adapter has not confirmed whole-record expiry',503)

def _delete(worker,row,ready):
    db=worker.db;db.execute('BEGIN IMMEDIATE')
    try:
        if _row(db,row[0])!=row or not ready():raise Fault('Completed record changed during expiry',409)
        for table in TABLES:db.execute('DELETE FROM '+table+' WHERE id=?',(row[0],))
        db.commit()
    except BaseException:db.rollback();raise

def live_ids(worker,inventory=None):
    if inventory is None:inventory=worker.remote.call('provider.poll',{'provider':worker.remote.node})
    if not isinstance(inventory,dict) or not isinstance(inventory.get('actions'),list):raise Fault('Current operation inventory unavailable',503)
    if len(inventory['actions'])>=64:raise Fault('Current operation inventory is truncated; absence unproven',409)
    live={a['id'] for a in inventory['actions'] if isinstance(a,dict) and isinstance(a.get('id'),str)}
    if len(live)!=len(inventory['actions']):raise Fault('Malformed current operation inventory',503)
    return live

def expire(worker,limit=8,after='',inventory=None):
    """Future/previously compacted records: genuine ACK, no remaining debug copy."""
    if type(limit) is not int or not 1<=limit<=64:raise ValueError('Bounded expiry page required')
    db=worker.db;live=live_ids(worker,inventory)
    rows=db.execute("SELECT a.id,a.signature,a.state,CASE WHEN a.result='{}' THEN '{}' ELSE NULL END,a.resource FROM result_acknowledged k "
        "JOIN actions a ON a.id=k.id WHERE k.id>? ORDER BY k.id LIMIT ?",(after,limit)).fetchall()
    deleted=[];protected=[]
    for row in rows:
        operation,signature,state,body,guard=row;after=operation
        if operation in live or state not in ('succeeded','failed') or body!='{}':
            protected.append([operation,'unresolved_or_not_compacted']);continue
        def ready():
            return (db.execute('SELECT 1 FROM result_acknowledged WHERE id=?',(operation,)).fetchone() and
                not any(db.execute('SELECT 1 FROM '+table+' WHERE id=?',(operation,)).fetchone()
                        for table in ('result_debug','reconciliation_debug','result_cleanup')))
        if not ready():protected.append([operation,'retained_window_or_pending_cleanup']);continue
        _adapter(worker,row);_delete(worker,row,ready);deleted.append(operation)
    return {'deleted':deleted,'protected':protected,'after':after,'more':len(rows)==limit}

def historical(worker,limit=16,after=0,progress=None):
    """Explicit old-record cleanup; local completion, fresh live inventory, no ACK fiction.

    Acknowledged records use expire(). This path handles older full results that
    predate ACK tracking. Current delivery queues and uncertain records stay put.
    Original insertion order supplies the existing 100-record/1MiB debug window.
    """
    from .worker import RESULT_DEBUG_COUNT,RESULT_DEBUG_BYTES
    from .worker_history import quiescent
    if type(limit) is not int or not 1<=limit<=64 or type(after) is not int or after<0:raise ValueError('Bounded historical expiry page required')
    db=worker.db
    live=live_ids(worker)
    recent=db.execute('SELECT rowid,id,state,length(CAST(result AS BLOB)) FROM actions ORDER BY rowid DESC LIMIT 1024').fetchall()
    completed=[r for r in recent if r[2] in ('succeeded','failed')]
    if len(recent)==1024 and len(completed)<RESULT_DEBUG_COUNT:raise Fault('Recent completed window needs further bounded classification',409)
    keep=set();used=0
    for unused,op,state,amount in completed[:RESULT_DEBUG_COUNT]:
        debug=db.execute('SELECT bytes FROM result_debug WHERE id=?',(op,)).fetchone()
        used+=debug[0] if debug else amount
        if used<=RESULT_DEBUG_BYTES:keep.add(op)
    rows=db.execute('SELECT rowid,id,signature,state,length(CAST(result AS BLOB)),resource FROM actions WHERE rowid>? ORDER BY rowid LIMIT ?',(after,limit)).fetchall()
    deleted=[];protected=[];used=0
    def report():
        if progress is not None:progress({'deleted':list(deleted),'protected':[list(x) for x in protected],'after':after,'more':True})
    for order,op,sig,state,amount,guard in rows:
        previous=after;after=order
        if used+amount>1048576:protected.append([op,'page_payload_budget']);report();continue
        if op in live or op in keep or state not in ('succeeded','failed'):
            protected.append([op,'current_unresolved_or_retained_window']);report();continue
        if db.execute('SELECT 1 FROM result_acknowledged WHERE id=?',(op,)).fetchone():
            protected.append([op,'use_acknowledged_expiry']);report();continue
        body=db.execute('SELECT result FROM actions WHERE id=?',(op,)).fetchone()[0];used+=amount;row=(op,sig,state,body,guard)
        result=json.loads(body)
        if not quiescent(result):protected.append([op,'not_terminal_quiescent']);report();continue
        def ready():
            return not any(db.execute('SELECT 1 FROM '+t+' WHERE id=?',(op,)).fetchone() for t in ('result_recovery_pending','result_cleanup','result_acknowledged'))
        if not ready():protected.append([op,'pending_delivery_or_cleanup']);report();continue
        if progress is not None:
            progress({'deleted':list(deleted),'protected':[list(x) for x in protected],
                      'after':previous,'more':True,'inflight':{'operation':op,'cursor':order,
                      'row_sha256':hashlib.sha256(encode(list(row)).encode()).hexdigest(),
                      'signature':sig,'state':state,'result_sha256':hashlib.sha256(body.encode()).hexdigest()}})
        try:_adapter(worker,row,result)
        except AdapterExpiryNonzero as error:
            protected.append([op,'adapter_nonzero_exit:'+str(error.returncode)]);report();continue
        _delete(worker,row,ready);deleted.append(op);report()
    return {'deleted':deleted,'protected':protected,'after':after,'more':len(rows)==limit}
