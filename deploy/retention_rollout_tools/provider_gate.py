"""Temporary offer pause using an already-initialized signed provider Remote.
No discovery, enrollment, credentials, cancellation, claims or result mutations.
Call acquire only from an independent job after its dispatch command finishes.
Call restore from finally after apply/rollback. Persist saved_offers before acquire.
"""
import time
FIELDS=('provider','contract','resource','service','configuration','capacity','state')
def poll(remote):
    q=remote.call('provider.poll',{'provider':remote.node})
    if any(k not in q or not isinstance(q[k],list) for k in ('actions','fences','activations')):
        raise RuntimeError('Incomplete provider poll; cannot prove drained')
    return q
def empty(q): return not (q['actions'] or q['fences'] or q['activations'])
def offer(remote, original, state):
    if original.get('provider')!=remote.node:
        raise RuntimeError('Offer belongs to another provider')
    # Original must come from a verified current offer snapshot, not inferred defaults.
    if not all(k in original for k in FIELDS):
        raise RuntimeError('Need exact current offer fields before temporary pause')
    args={k:original[k] for k in FIELDS};args['state']=state
    if 'usage' in original: args['usage']=original['usage']
    result=remote.call('cap.offer',args)
    if any(result.get(k)!=args[k] for k in FIELDS):
        raise RuntimeError('Offer update response differs from requested fields')
    return result
def restore(remote, saved_offers):
    errors=[]
    for original in saved_offers:
        try: offer(remote,original,original['state'])
        except Exception as e: errors.append(str(e))
    if errors: raise RuntimeError('Offer restoration failed: '+str(errors))
def acquire(remote,saved_offers,timeout=600):
    if not saved_offers or any(o['state']!='ready' for o in saved_offers):
        raise RuntimeError('Only existing ready offers can be temporarily paused')
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if not empty(poll(remote)):
            time.sleep(1);continue
        try:
            for original in saved_offers:offer(remote,original,'offline')
            # Queued work racing the pause cannot claim offline. Restore ready and retry.
            if empty(poll(remote)):
                return {'admission_closed':True,'active_commands':0}
        except BaseException:
            restore(remote,saved_offers);raise
        restore(remote,saved_offers);time.sleep(1)
    raise RuntimeError('Provider did not reach a drained boundary; offers left ready')
