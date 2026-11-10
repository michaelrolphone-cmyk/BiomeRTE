"""Explicit, bounded historical result enrollment. Never replay an operation.

Authority is queried through the worker's authenticated Remote. Plans are exact
preimages, not permission tokens. Applying re-reads authority and local rows.
Legacy rows use their original insertion order below newer debug sequence IDs.
No timestamp is manufactured and identity fences are never removed.
"""
import json
from .store import digest, encode, Fault


def quiescent(result):
    return isinstance(result, dict) and (result.get('quiescent') is True or result.get('evidence', {}).get('quiescent') is True or result.get('reconciliation', {}).get('quiescent') is True)


def _confirmed(worker, operation, signature, state, result):
    action = worker.remote.call('cap.status', {'operation': operation, 'recovery': True})
    if not isinstance(action, dict) or action.get('id') != operation or action.get('provider') != worker.remote.node or action.get('state') != state:
        return None
    expired = action.get('result_expired') is True
    try:
        identity = action.get('identity_digest') if expired else digest([action[k] for k in ('contract', 'inputs', 'resource', 'generation', 'lease')])
    except KeyError:
        return None
    value = action.get('result_digest') if expired else digest(action.get('result'))
    if identity != signature or value != digest(result) or not quiescent(result):
        return None
    return action


def plan(worker, after=0, limit=32):
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 64:
        raise ValueError('Invalid historical enrollment page')
    # Legacy must precede the first tracked action, not merely lack a marker.
    history_exists = worker.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='historical_result_enrolled'").fetchone()
    exclusion = ' WHERE NOT EXISTS(SELECT 1 FROM historical_result_enrolled h WHERE h.id=a.id)' if history_exists else ''
    boundary = worker.db.execute('SELECT MIN(a.rowid) FROM actions a JOIN result_retention_eligible e ON e.id=a.id AND e.signature=a.signature' + exclusion).fetchone()[0]
    if boundary is None:
        boundary = worker.db.execute('SELECT COALESCE(MAX(rowid),0)+1 FROM actions').fetchone()[0]
    rows = worker.db.execute('SELECT rowid,id,signature,state,result,resource FROM actions WHERE rowid>? AND rowid<? ORDER BY rowid LIMIT ?', (after,boundary,limit)).fetchall()
    items, excluded, used, cursor = [], [], 0, after
    for rowid, operation, signature, state, body, resource in rows:
        amount = len(body.encode('utf-8'))
        if used + amount > 1024 * 1024 and used:
            break
        cursor = rowid
        used += amount
        if amount > 1024 * 1024:
            excluded.append({'id': operation, 'reason': 'oversized_requires_separate_review'})
            continue
        if worker.db.execute('SELECT 1 FROM result_acknowledged WHERE id=?',(operation,)).fetchone():
            continue
        result = json.loads(body)
        if state not in ('succeeded','failed') or not isinstance(result,dict) or not quiescent(result):
            excluded.append({'id':operation,'reason':'not_terminal_quiescent'})
            continue
        action = _confirmed(worker,operation,signature,state,result)
        if action is None:
            excluded.append({'id':operation,'reason':'authority_not_exact'})
            continue
        items.append({'rowid':rowid,'id':operation,'signature':signature,'state':state,'result':body,'resource':resource,'debug_seq':rowid-(1 << 62)})
    return {'format':'worker-history/1','items':items,'excluded':excluded,'after':cursor,'more':bool(rows) and (len(rows)==limit or cursor != rows[-1][0])}


def enroll(worker, operation, signature, state, body, item):
    """Inside retire_result's transaction; all local preimages must still match."""
    if item.get('id') != operation or item.get('signature') != signature or item.get('state') != state or item.get('result') != body:
        raise Fault('Historical result manifest mismatch',409)
    row = worker.db.execute('SELECT rowid,id,signature,state,result,resource FROM actions WHERE id=?',(operation,)).fetchone()
    if row != tuple(item[k] for k in ('rowid','id','signature','state','result','resource')) or item['debug_seq'] != item['rowid']-(1 << 62):
        raise Fault('Historical result changed',409)
    worker.db.execute('INSERT OR IGNORE INTO historical_result_enrolled VALUES(?,?)',(operation,item['rowid']))
    worker.db.execute('INSERT OR IGNORE INTO result_retention_eligible VALUES(?,?)',(operation,signature))
    audit = worker.db.execute('SELECT state,result FROM reconciliations WHERE id=?',(operation,)).fetchone()
    if audit == (state,body):
        worker.db.execute('INSERT OR IGNORE INTO reconciliation_retention_eligible VALUES(?,?,?,?,NULL)',(operation,signature,'historical-exact-authority',digest(json.loads(body))))


def apply(worker, manifest):
    if manifest.get('format') != 'worker-history/1' or len(manifest.get('items',[])) > 64:
        raise ValueError('Invalid historical enrollment manifest')
    if sum(len(item['result'].encode('utf-8')) for item in manifest['items']) > 1024 * 1024:
        raise ValueError('Historical enrollment byte budget exceeded')
    completed = []
    for item in manifest['items']:
        retired = worker.db.execute('SELECT a.rowid,a.signature,a.state,a.result,h.row_order FROM actions a JOIN historical_result_enrolled h ON h.id=a.id JOIN result_acknowledged k ON k.id=a.id WHERE a.id=?',(item['id'],)).fetchone()
        if retired == (item['rowid'],item['signature'],item['state'],'{}',item['rowid']):
            completed.append(item['id'])
            continue
        result = json.loads(item['result'])
        action = _confirmed(worker,item['id'],item['signature'],item['state'],result)
        if action is None:
            raise Fault('Historical authority changed',409)
        adapter = next((o for o in worker.offers if o['contract']==action.get('contract') and o['resource']==action.get('resource')), {})
        worker.retire_result(item['id'],item['signature'],item['state'],result,adapter.get('acknowledge_argv'),legacy=item)
        completed.append(item['id'])
    return {'retired':completed}
