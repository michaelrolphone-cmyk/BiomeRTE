"""Stateless MCP JSON-RPC endpoint with discovery and a stable bus invocation gateway."""
from .api import OPERATIONS
from .store import Fault, encode


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
                         'capabilities': app.call('cap.discover')}
            elif params.get('name') == 'invoke':
                value = app.call(args['operation'], args['arguments'],
                                 args.get('request_id'), args.get('realm', 'site'))
            else:
                raise Fault('Unknown tool')
            result = {'content': [{'type': 'text', 'text': encode(value)}]}
        except (Fault, KeyError, ValueError, TypeError) as e:
            result = {'isError': True, 'content': [{'type': 'text', 'text': str(e)}]}
    else:
        return {'jsonrpc': '2.0', 'id': ident, 'error': {'code': -32601, 'message': 'Method not found'}}
    return {'jsonrpc': '2.0', 'id': ident, 'result': result}
