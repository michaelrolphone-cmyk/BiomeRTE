"""Stateless MCP JSON-RPC endpoint with discovery and a stable bus invocation gateway."""
from .api import OPERATIONS
from .store import Fault, encode, digest


CALLER_GUIDE = {
    'capability_usage': 'Read the selected offer.usage for its prerequisites, argument example, call sequence and result interpretation. A contract.usage applies across providers.',
    'caller': ['cap.discover', 'cap.acquire', 'cap.lease', 'cap.invoke', 'cap.status', 'cap.release'],
    'provider_only': ['provider.poll', 'cap.fenced', 'provider.claim', 'provider.result'],
    'lease': 'Use a distinct task ID per session. Queued means waiting for capacity; fencing means the provider is preparing the slot; bound means usable. Subscribe to lease.available using the reply_to inbox supplied to cap.acquire.',
    'command': 'Use a fresh operation ID for each logical command. Repeat it only to retry the same command. Read cap.status with that ID until succeeded, failed, or uncertain. The result is retained after release.',
    'outstanding_work': 'provider.poll lists outstanding work for a worker. Empty means nothing is outstanding; it does not describe the result of a completed command.'
}


def caller_view(op, value, realm):
    if op == 'provider.poll':
        return dict(value, purpose=CALLER_GUIDE['outstanding_work'])
    if op not in ('cap.acquire', 'cap.lease', 'cap.invoke', 'cap.status'):
        return value
    result = dict(value)
    key = result['id']
    if op in ('cap.acquire', 'cap.lease'):
        if result['state'] not in ('queued', 'fencing'):
            result['next_action'] = ('Submit a command with cap.invoke.' if result['state'] == 'bound'
                                     else 'The lease is released.')
            return result
        operation, args = 'cap.lease', {'lease': key}
        result['next_action'] = 'Wait for bound. The provider acknowledges its own fence.'
    else:
        operation, args = 'cap.status', {'operation': key}
        result['next_action'] = ('Read the result below.' if result['state'] in ('succeeded', 'failed')
                                 else 'Reconcile the uncertain effect before retrying.' if result['state'] == 'uncertain'
                                 else 'Read cap.status for the result. The same operation ID follows this command to completion.')
    result['next_call'] = {'operation': operation, 'arguments': args, 'realm': realm,
                           'request_id': 'read-' + digest([operation, key, realm])[:32]}
    return result


def respond(app, body, admin):
    if not admin:
        raise Fault('Controller credential required', 401)
    ident = body.get('id')
    method = body.get('method', '')
    if body.get('jsonrpc') != '2.0':
        raise Fault('Expected JSON-RPC 2.0')
    if method.startswith('notifications/'):
        return None
    result = None
    if method == 'initialize':
        result = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}},
                  'serverInfo': {'name': 'BiomeRTE', 'version': '0.1.0'}}
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        result = {'tools': [
            {'name': 'discover', 'description': 'Read live Biome service contracts and capability offers.',
             'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
            {'name': 'invoke', 'description': 'Invoke a Biome service. Reuse request_id on retries of a mutation.',
             'inputSchema': {'type': 'object', 'required': ['operation', 'arguments'], 'properties': {
                 'operation': {'type': 'string'}, 'arguments': {'type': 'object'}, 'request_id': {'type': 'string'},
                 'realm': {'type': 'string', 'enum': ['site', 'local']}}}}]}
    elif method == 'tools/call':
        params = body.get('params', {})
        args = params.get('arguments', {})
        try:
            if params.get('name') == 'discover':
                value = {'operations': app.call('bus.discover'),
                         'capabilities': app.call('cap.discover'), 'capability_caller_guide': CALLER_GUIDE}
            elif params.get('name') == 'invoke':
                value = app.call(args['operation'], args['arguments'],
                                 args.get('request_id'), args.get('realm', 'site'))
                value = caller_view(args['operation'], value, args.get('realm', 'site'))
            else:
                raise Fault('Unknown tool')
            result = {'content': [{'type': 'text', 'text': encode(value)}]}
        except (Fault, KeyError, ValueError, TypeError) as e:
            result = {'isError': True, 'content': [{'type': 'text', 'text': str(e)}]}
    else:
        return {'jsonrpc': '2.0', 'id': ident, 'error': {'code': -32601, 'message': 'Method not found'}}
    return {'jsonrpc': '2.0', 'id': ident, 'result': result}
