"""Coordinate one historical diagnostic page without executing any command.

Caller supplies the installed adapter/config and authenticated worker Remote.
Plans may be persisted privately, then apply revalidates exact authority and
preimages. Partial completion is retryable; command execution is never invoked.
"""
from . import worker_history, terminal_history
from .store import Fault


def _plan(worker, kind, adapter=None, cfg=None, terminal_db=None, home=None, after=0, limit=16):
    if kind not in ('mac','pi'):
        raise ValueError('Unknown terminal host kind')
    page=worker_history.plan(worker,after,limit)
    result={'format':'historical-terminal-page/1','kind':kind,'worker':dict(page,items=[]),'adapters':[],'excluded':list(page['excluded'])}
    for item in page['items']:
        import json
        value=json.loads(item['result'])
        # A validated argv rejection made no filesystem receipt or child.
        if value.get('evidence',{}).get('executed') is False:
            result['worker']['items'].append(item)
            continue
        try:
            evidence=terminal_history.mac_plan(adapter,cfg,worker.remote,item['id'],known=item) if kind=='mac' else terminal_history.pi_plan(terminal_db,home,worker.remote,item['id'],known=item)
        except (OSError, ValueError, KeyError, RuntimeError, Fault) as error:
            result['excluded'].append({'id':item['id'],'reason':'adapter_evidence_unavailable','error':str(error)[:240]})
            continue
        result['adapters'].append(evidence)
        result['worker']['items'].append(item)
    return result


def _apply(worker, manifest, adapter=None, cfg=None, terminal_db=None, home=None):
    if manifest.get('format') != 'historical-terminal-page/1' or manifest.get('kind') not in ('mac','pi'):
        raise ValueError('Invalid historical terminal page')
    import json
    items=manifest['worker']['items']
    planned=[item['id'] for item in items]
    adapters=[item['operation'] for item in manifest['adapters']]
    expected=[item['id'] for item in items if json.loads(item['result']).get('evidence',{}).get('executed') is not False]
    if (len(planned)>64 or len(planned)!=len(set(planned)) or
            len(adapters)!=len(set(adapters)) or set(adapters)!=set(expected)):
        raise ValueError('Historical adapter page mismatch')
    known={item['id']:item for item in items}
    for item in manifest['adapters']:
        if manifest['kind']=='mac':
            terminal_history.mac_apply(adapter,cfg,worker.remote,item,known=known[item['operation']])
        else:
            terminal_history.pi_apply(terminal_db,home,worker.remote,item,known=known[item['operation']])
    retired=worker_history.apply(worker,manifest['worker'])
    # Durable ACK cleanup uses only the installed adapter's acknowledgement path.
    # It does not launch/replay an operation. Failed cleanup remains retryable.
    for operation in retired['retired']:
        worker.cleanup_results(operation)
    return retired


def plan(worker,kind,adapter=None,cfg=None,terminal_db=None,home=None,after=0,limit=16):
    from .history_authority import scoped_worker
    return _plan(scoped_worker(worker),kind,adapter,cfg,terminal_db,home,after,limit)


def apply(worker,manifest,adapter=None,cfg=None,terminal_db=None,home=None):
    from .history_authority import scoped_worker
    # A new authority scope is mandatory, never reuse the dry-run snapshot.
    return _apply(scoped_worker(worker),manifest,adapter,cfg,terminal_db,home)
