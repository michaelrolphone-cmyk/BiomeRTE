"""Explicit session lifecycle operations through the ordinary authenticated API.

The local request receipt is reserved before external I/O. An uncertain request
is never automatically repeated. Session history and signed outboxes remain in
the existing bounded runtime and are discarded after settlement.
"""
import time
from .api import op, OPERATIONS, validate, ident
from .store import Fault, digest, text
from . import retention

EXTERNAL = {
    'session.state': ('', False),
    'session.retire_request': ('request_id:str', True),
    'session.open': ('scope:list participants:list baselines:obj peer_keys:obj policy?:obj authorized_closers?:list', True),
    'session.append': ('session:str expected_seq:int changes:list', True),
    'session.invite': ('session:str recipient:str', True),
    'session.synchronize': ('session:str', True),
    'session.finalize': ('session:str', True),
    'session.deliver': ('session:str', True),
    'session.recover_open': ('session:str', True),
    'session.resume': ('session:str', True),
    'session.inspect_reconciliation': ('', False),
    'session.reconcile_packet': ('packet:obj', True),
}
DESCRIPTIONS = {
    'session.state': 'Read the current bounded session summary without snapshots or keys.',
    'session.retire_request': "Retire this caller's uncertain API receipt without replay or any assertion of success. Session reconciliation remains required.",
    'session.open': 'Explicitly open a bounded durable scope for exact trusted participants and baselines.',
    'session.append': 'Append explicit durable changes to this session at the expected sequence. Ordinary writes remain local and fenced.',
    'session.invite': 'Deliver a signed invitation to an already authorized participant.',
    'session.synchronize': 'Reconcile the current session head without repeating an uncertain append.',
    'session.finalize': 'Freeze, sign final outcome packages and delete the temporary shared session.',
    'session.deliver': 'Deliver pending signed outcome packages and settle their acknowledgements.',
    'session.recover_open': 'Reconcile an uncertain opening through status without repeating open.',
    'session.resume': 'Explicitly clear this session authorization-denied latch after authority is restored.',
    'session.inspect_reconciliation': 'Read signed bounded reconciliation facts for the current session.',
    'session.reconcile_packet': 'Reconcile an explicitly supplied signed coordination packet.',
}

def register(name, fields, write):
    def external(tx, ctx, **arguments):
        raise Fault('Session lifecycle operations require a top-level controller call', 409)
    external.__doc__ = DESCRIPTIONS[name]
    op(name, fields, write)(external)
for _name, (_fields, _write) in EXTERNAL.items():
    register(_name, _fields, _write)

@op('session.baseline', 'scope:list', False)
def baseline(tx, ctx, scope):
    """Read this controller's exact durable baseline and public participant identity."""
    if ctx.scope != 'site' or ctx.app.stores['site'].remote is not None:
        raise Fault('Sessions require the local site realm', 409)
    from .session_exchange import validate_scope, identity
    validate_scope(scope)
    return {'node':ctx.app.node,'site':ctx.app.site,'public_key':ctx.app.config['public_key'],
            'scope':scope,'baseline':{identity(kind,key):digest(tx.get(kind,key)) for kind,key in scope}}

def summary(value):
    if isinstance(value, dict) and 'id' in value and 'scope' in value and 'participants' in value:
        fields = ('id','state','scope','participants','owner','authorized_closers','expires',
                  'seq','hash','bytes','policy','closer','closer_epoch','closer_until',
                  'authorization_block','needs_close','outbox_ready','final_head')
        return {key:value[key] for key in fields if key in value}
    return value

def dispatch(app, name, arguments, request_id, realm, authorize):
    if realm != 'site' or app.stores['site'].remote is not None:
        raise Fault('Sessions require the local site realm', 409)
    write = EXTERNAL[name][1]
    store = app.stores['site']
    def check(tx):
        ctx = authorize(tx)
        if not ctx.allowed(name):
            raise Fault('Permission denied', 403)
        validate(OPERATIONS[name][1], arguments)
        return ctx
    ctx = store.execute(check, read_only=True)
    if write:
        ident(text(request_id, 'request_id'))
    service = getattr(app, 'session_service', None)
    if service is None:
        from .session_service import configured_service
        service = configured_service(app)
        app.session_service = service
    from .session_runtime import serialized
    runtime = service.runtime()
    @serialized
    def run(runtime):
        key, signature = digest([ctx.actor, request_id]), digest([name, arguments])
        if write:
            def reserve(tx):
                check(tx)
                old = tx.get('_receipt', key)
                if old:
                    if old['signature'] != signature:
                        raise Fault('Request ID reused with different arguments', 409)
                    if old.get('result_expired'):
                        raise Fault('Receipt result retired; request will not execute again', 410)
                    if old.get('external_pending'):
                        raise Fault('Session request outcome is uncertain; inspect session.state and use explicit recovery with a new request ID', 409)
                    return old
                pending = tx.db.execute("SELECT count(*) FROM records WHERE kind='_receipt' AND instr(value,'\"external_pending\":true')>0").fetchone()[0]
                if pending >= 64 and name != 'session.retire_request':
                    raise Fault('Unresolved session request receipt limit reached', 429)
                tx.put('_receipt', key, {'signature':signature,'external_pending':True,
                    'format':2,'operation':name,'created_at':time.time()})
                return None
            old = store.execute(reserve)
            if old:
                if old.get('external_error'):
                    raise Fault(old['external_error']['message'], old['external_error']['status'])
                return old['result']
        def finish(result=None, error=None):
            def commit(tx):
                old = tx.need('_receipt', key)
                if old['signature'] != signature or not old.get('external_pending'):
                    raise Fault('Session request receipt changed', 409)
                value = dict(old)
                value.pop('external_pending', None)
                if error:
                    value['external_error'] = {'message':str(error),'status':error.status}
                else:
                    value['result'] = result
                # Pending intents were deliberately not enrolled as completed.
                # Enroll the finished normal local receipt using existing retention.
                tx.drop('_receipt', key)
                if app.config.get('operational_retention', {}).get('enabled', True):
                    with retention.tracking(tx):
                        tx.put('_receipt', key, value)
                else:
                    tx.put('_receipt', key, value)
            store.execute(commit)
        operation = name.split('.', 1)[1]
        inputs = dict(arguments)
        try:
            if operation == 'retire_request':
                target = digest([ctx.actor, ident(text(inputs['request_id'], 'request_id'))])
                if target == key:
                    raise Fault('Cannot retire the current request', 409)
                def retire(tx):
                    check(tx)
                    original = tx.need('_receipt', target)
                    if not original.get('external_pending'):
                        return {'retired':False, 'request_id':inputs['request_id']}
                    value = dict(original)
                    value.pop('external_pending', None)
                    value['external_error'] = {'message':'Uncertain session request retired without replay; use session reconciliation to establish its effects', 'status':409}
                    tx.drop('_receipt', target)
                    if app.config.get('operational_retention', {}).get('enabled', True):
                        with retention.tracking(tx):
                            tx.put('_receipt', target, value)
                    else:
                        tx.put('_receipt', target, value)
                    return {'retired':True, 'request_id':inputs['request_id'], 'outcome':'uncertain', 'replayed':False}
                result = store.execute(retire)
                finish(result=result)
                return result
            expected_session = inputs.pop('session', None)
            if expected_session is not None:
                state = runtime.state()
                if not state or state['id'] != expected_session:
                    raise Fault('Session identity changed or is no longer active', 409)
            if operation == 'append':
                expected_seq = inputs.pop('expected_seq')
                state = service.control('synchronize', {})
                if state['seq'] != expected_seq:
                    raise Fault('Session sequence changed; inspect current state before proposing changes', 409)
            result = summary(service.control(operation, inputs))
        except Fault as error:
            if write and error.status < 500:
                finish(error=error)
            raise
        if write:
            finish(result=result)
        return result
    return run(runtime)
