"""Caller-facing instructions assembled from the contract and its publisher."""
from .api import validate
from .store import Fault, encode, text

ECHO_USAGE = {
    'parameters': {'value': 'Any JSON value to echo.'},
    'example_inputs': {'value': 'hello'},
    'read_result': 'Read cap.status.result.value. For this example it is "hello".'
}


def published_usage(contract, usage):
    allowed = {'parameters', 'example_inputs', 'before', 'read_result', 'errors'}
    if set(usage) - allowed or len(encode(usage).encode('utf-8')) > 8192:
        raise Fault('Invalid capability usage fields or size')
    for field in ('before', 'read_result', 'errors'):
        if field in usage:
            text(usage[field], field, 2048)
    if 'parameters' in usage:
        params = usage['parameters']
        if not isinstance(params, dict) or set(params) - set(contract['inputs']):
            raise Fault('Usage parameter descriptions must name declared inputs')
        for description in params.values():
            text(description, 'parameter description', 1024)
    if 'example_inputs' in usage:
        validate(contract['inputs'], usage['example_inputs'])
    return usage


def caller_usage(contract, guide, resource=None):
    # Published examples retain native JSON types. For undocumented contracts
    # present explicit typed slots, never fabricated domain-valid values.
    args = {'task': '<unique session ID>', 'contract': contract['id']}
    if resource:
        args['resource'] = resource
    inputs = guide.get('example_inputs')
    if inputs is None:
        inputs = {k: '<supply %s: %s>' % (k, v['type'])
                  for k, v in contract['inputs'].items() if v['required']}
    calls = [
        {'operation': 'cap.acquire', 'arguments': args, 'request_id': '<acquire request ID>'},
        {'operation': 'cap.lease', 'arguments': {'lease': '<acquire.id>'}, 'request_id': '<lease read ID>'},
        {'operation': 'cap.invoke', 'arguments': {'lease': '<acquire.id>', 'operation': '<unique command ID>',
                                                 'inputs': inputs}, 'request_id': '<invoke request ID>'},
        {'operation': 'cap.status', 'arguments': {'operation': '<same command ID>'}, 'request_id': '<status read ID>'},
        {'operation': 'cap.release', 'arguments': {'lease': '<acquire.id>'}, 'request_id': '<release request ID>'}
    ]
    fields = ', '.join(sorted(contract['outputs'])) or '(empty object)'
    result = {'calls': calls, 'inputs_kind': 'example' if 'example_inputs' in guide else 'template',
              'instructions': 'Use these arguments with biome_invoke in the same realm. Replace angle-bracket bindings. '
                              'Wait for a bound lease before invoking. Follow the invocation next_call to cap.status. '
                              'Inspect result on succeeded, inspect the failure on failed, and reconcile uncertain effects before retry. '
                              'Release when finished. Reuse a mutation request ID only for an identical retry.',
              'read_result': guide.get('read_result', 'On succeeded, read cap.status.result. Declared output fields: ' + fields)}
    for field in ('parameters', 'before', 'errors'):
        if field in guide:
            result[field] = guide[field]
    return result
