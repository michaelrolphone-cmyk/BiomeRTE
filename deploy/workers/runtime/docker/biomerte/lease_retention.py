"""Bounded released-lease observation and retirement guards.

Existing leases without a recorded release time get a genuine observed-before
upper bound, never an invented release date. Current fencing state is untouched.
"""
import time
from .store import digest

def safe_released(tx,key,value):
    if not isinstance(value,dict) or value.get('id')!=key or value.get('state')!='released':return False
    guard=tx.get('guards',value.get('guard','')) if value.get('guard') else None
    if guard and guard.get('holder')==key:return False
    activity=tx.get('_reservation_activity',key)
    if activity is not None and (not isinstance(activity,dict) or activity.get('generation')!=value.get('generation')):return False
    # Indexed reference reachability also protects any current or unknown owner.
    return True

def observe(tx,after='',limit=64,now=None):
    if type(limit) is not int or not 1<=limit<=64:raise ValueError('Bounded lease page required')
    now=time.time() if now is None else now
    rows=tx.db.execute("SELECT id,length(CAST(value AS BLOB)) FROM records WHERE kind='leases' AND id>? ORDER BY id LIMIT ?",(after,limit)).fetchall()
    observed=[];protected=[];used=0
    for key,size in rows:
        after=key
        if size>32768 or used+size>1048576:protected.append([key,'payload_budget']);continue
        value=tx.get('leases',key);used+=size
        if not safe_released(tx,key,value):protected.append([key,'current_or_unproven']);continue
        marker='leases:'+key
        existing=tx.get('_retention',marker)
        if existing is not None and ('terminal_at' in existing or existing.get('observation_digest')==digest(value)):continue
        tx.put('_retention',marker,{'id':marker,'kind':'leases','key':key,'historical':True,
            'observed_before':now,'observation_digest':digest(value)})
        observed.append(key)
    return {'observed':observed,'protected':protected,'after':after,'more':len(rows)==limit}

def disposable_guard(tx,key,value):
    """Only the exact per-lease shared guard, never a reusable resource fence."""
    guard_key=value.get('guard')
    if guard_key!='!shared:'+digest([value.get('resource'),key]):return None
    guard=tx.get('guards',guard_key)
    if not isinstance(guard,dict) or guard.get('holder') or guard.get('generation')!=value.get('generation'):return None
    from .retention_index import protected_by_roots
    identities={('leases',key):value,('guards',guard_key):guard}
    if ('guards',guard_key) in protected_by_roots(tx,identities,identities):return None
    return guard_key

def observe_receipts(tx,after='',limit=64):
    """Use genuine format-2 receipt timestamps as released-before evidence."""
    import math
    if type(limit) is not int or not 1<=limit<=64:raise ValueError('Bounded receipt page required')
    rows=tx.db.execute("SELECT id,length(CAST(value AS BLOB)) FROM records WHERE kind='_receipt' AND id>? ORDER BY id LIMIT ?",(after,limit)).fetchall()
    observed=[];used=0
    for ident,size in rows:
        after=ident
        if size>32768 or used+size>1048576:continue
        receipt=tx.get('_receipt',ident);used+=size
        if not isinstance(receipt,dict):continue
        stamp=receipt.get('created_at');value=receipt.get('result')
        if receipt.get('format')!=2 or type(stamp) not in (int,float) or not math.isfinite(stamp) or stamp<0 or not isinstance(value,dict):continue
        key=value.get('id')
        if not isinstance(key,str) or not safe_released(tx,key,value):continue
        current=tx.get('leases',key)
        if current!=value:continue
        marker='leases:'+key;old=tx.get('_retention',marker,{})
        if 'terminal_at' in old or old.get('observed_before',float('inf'))<=stamp:continue
        tx.put('_retention',marker,{'id':marker,'kind':'leases','key':key,'historical':True,
            'observed_before':stamp,'observation_digest':digest(current),'evidence_receipt_digest':digest(receipt)})
        observed.append(key)
    return {'observed':observed,'after':after,'more':len(rows)==limit}
