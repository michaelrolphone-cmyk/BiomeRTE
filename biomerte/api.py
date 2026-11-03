"""Runtime service directory and transaction-bound command dispatch."""
import re
from .store import Fault, digest, text, uid

OPERATIONS = {}
TYPES = {'str': str, 'obj': dict, 'list': list, 'int': int, 'bool': bool,
         'num': (int, float), 'any': object}


def op(name, fields='', write=True):
    def register(fn):
        schema = {}
        for field in fields.split():
            key, typ = field.split(':')
            schema[key.rstrip('?')] = {'type': typ, 'required': not key.endswith('?')}
        OPERATIONS[name] = (fn, schema, write)
        return fn
    return register


def match(value, typ):
    return isinstance(value, TYPES[typ]) and not (typ in ('int', 'num') and isinstance(value, bool))


def validate(schema, args):
    if not isinstance(args, dict):
        raise Fault('Arguments must be an object')
    extra = set(args) - set(schema)
    if extra:
        raise Fault('Unknown argument: ' + sorted(extra)[0])
    for k, v in schema.items():
        if k not in args:
            if v.get('required'):
                raise Fault('Missing argument: ' + k)
        elif not match(args[k], v['type']):
            raise Fault('Wrong type for ' + k)


def schema(spec):
    if not isinstance(spec, dict):
        raise Fault('Schema must be an object')
    result = {}
    for name, typ in spec.items():
        if not isinstance(typ, str) or typ.rstrip('?') not in TYPES:
            raise Fault('Unknown field type')
        result[text(name)] = {'type': typ.rstrip('?'), 'required': not typ.endswith('?')}
    return result


def ident(value):
    if not re.match(r'^[A-Za-z0-9_.:-]{1,128}$', value) or '\n' in value:
        raise Fault('Invalid identifier')
    return value


class Context:
    def __init__(self, app, actor='admin', scope='site', permissions=None):
        self.app = app
        self.actor = actor
        self.scope = scope
        self.permissions = permissions

    def allowed(self, name):
        return self.permissions is None or name in self.permissions


def invoke(tx, ctx, name, args):
    if name not in OPERATIONS:
        raise Fault('Unknown operation: ' + name, 404)
    fn, fields, unused = OPERATIONS[name]
    if not ctx.allowed(name):
        raise Fault('Permission denied', 403)
    validate(fields, args)
    return fn(tx, ctx, **args)


def command(tx, ctx, name, args, request_id):
    if name not in OPERATIONS:
        raise Fault('Unknown operation', 404)
    if not ctx.allowed(name):
        raise Fault('Permission denied', 403)
    if not OPERATIONS[name][2]:
        return invoke(tx, ctx, name, args)
    ident(text(request_id, 'request_id'))
    key = digest([ctx.actor, request_id])
    signature = digest([name, args])
    old = tx.get('_receipt', key)
    if old:
        if old['signature'] != signature:
            raise Fault('Request ID reused with different arguments', 409)
        return old['result']
    result = invoke(tx, ctx, name, args)
    tx.put('_receipt', key, {'signature': signature, 'result': result})
    return result


@op('bus.discover', write=False)
def discover(tx, ctx):
    """List operations and their argument contracts for this caller."""
    return [{'name': name, 'description': fn.__doc__ or '', 'inputs': fields, 'write': write}
            for name, (fn, fields, write) in sorted(OPERATIONS.items()) if ctx.allowed(name)]


@op('bus.publish', 'topic:str payload:obj')
def publish(tx, ctx, topic, payload):
    """Publish a named application event. System topics are reserved for domain handlers."""
    if not topic.startswith('user.'):
        raise Fault('External events use user.* topics')
    event=tx.emit(text(topic),payload)
    from .knowledge import put
    put(tx,ctx,'event.'+event['id'],event,type='bus_event')
    return event


@op('bus.send', 'to:str body:obj correlation?:str')
def send(tx, ctx, to, body, correlation=''):
    """Deliver an addressable message, preserving sender and work correlation."""
    msg = {'id': uid(), 'from': ctx.actor, 'to': text(to), 'body': body,
           'correlation': correlation, 'read': False}
    tx.put('messages', msg['id'], msg)
    from .knowledge import put
    put(tx,ctx,'message.'+msg['id'],msg,type='mail')
    tx.emit('message.received', {'message': msg['id'], 'to': to})
    return msg


@op('bus.inbox', 'address?:str', False)
def inbox(tx, ctx, address=''):
    address = address or ctx.actor
    if ctx.permissions is not None and address != ctx.actor:
        raise Fault('Other inbox', 403)
    return [r for r in tx.rows('messages') if r['to'] == address]


@op('bus.ack_message', 'message:str')
def ack_message(tx, ctx, message):
    msg = tx.need('messages', message)
    if ctx.permissions is not None and msg['to'] != ctx.actor:
        raise Fault('Other inbox', 403)
    msg['read'] = True
    return tx.put('messages', message, msg)


@op('bus.subscribe', 'name:str topic:str process?:str from_seq?:int')
def subscribe(tx, ctx, name, topic, process='', from_seq=0):
    """Subscribe to one exact topic or *; an optional process starts for each event."""
    if process:
        tx.need('definitions', process)
    if process and topic == '*':
        raise Fault('A process trigger needs an exact topic')
    if process:
        validate(schema(tx.need('definitions', process)['inputs']), {'event': {}})
    key = ident(name)
    if tx.get('subscriptions', key):
        raise Fault('Subscription exists', 409)
    return tx.put('subscriptions', key, {'id': key, 'topic': topic, 'cursor': from_seq,
                                         'process': process, 'owner': ctx.actor})


def events_after(tx, cursor, topic, limit=100):
    import json
    sql = "SELECT value FROM records WHERE kind='events' AND id>? ORDER BY id"
    result = []
    for row in tx.db.execute(sql, ('%020d' % cursor,)):
        e = json.loads(row[0])
        if topic in ('*', e['topic']):
            result.append(e)
            if len(result) >= limit:
                break
    return result


@op('bus.poll', 'name:str limit?:int', False)
def poll(tx, ctx, name, limit=100):
    sub = tx.need('subscriptions', name)
    if ctx.permissions is not None and sub['owner'] != ctx.actor:
        raise Fault('Other subscription', 403)
    return events_after(tx, sub['cursor'], sub['topic'], max(1, min(limit, 200)))


@op('bus.ack', 'name:str seq:int')
def ack(tx, ctx, name, seq):
    sub = tx.need('subscriptions', name)
    if ctx.permissions is not None and sub['owner'] != ctx.actor:
        raise Fault('Other subscription', 403)
    if seq < sub['cursor'] or seq > tx.get('_meta', 'events', 0):
        raise Fault('Invalid subscription cursor')
    sub['cursor'] = seq
    return tx.put('subscriptions', name, sub)
