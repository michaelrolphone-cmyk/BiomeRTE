"""Bounded, transport-neutral callback envelopes for asynchronous capability work."""
import json
import os
from .api import ident, op, text, validate
from .store import Fault

NAME = 'capability-followup'
FIELDS = ('contract:str arguments:obj caller:str success_prompt:str failure_prompt:str resource?:str '
          'caller_resource?:str context?:obj success_equals?:obj')


@op('continuation.prepare', FIELDS, False)
def prepare(tx, ctx, contract, arguments, caller, success_prompt, failure_prompt,
            resource='', caller_resource='', context=None, success_equals=None):
    """Validate the continuation address and bounded correlation metadata before starting work."""
    ident(contract)
    ident(caller)
    if resource:
        ident(resource)
    if caller_resource:
        ident(caller_resource)
    receiver = tx.need('contracts', caller)
    context = {} if context is None else context
    if len(json.dumps(context)) > 8192:
        raise Fault('Caller context exceeds 8 KiB; pass a reference to larger state')
    for prompt in (success_prompt, failure_prompt):
        text(prompt, 'follow-up prompt', 8000)
        validate(receiver['inputs'], {'request': prompt, 'context': {}})
    if success_equals is not None and (len(success_equals) > 32 or any(
            not path or len(path) > 256 or not all(path.split('.')) for path in success_equals)):
        raise Fault('success_equals needs at most 32 nonempty dotted result paths')
    return dict(resource=resource, caller_resource=caller_resource, context=context, success_equals=success_equals)


@op('cap.run_async', FIELDS)
def run_async(tx, ctx, **args):
    """Start capability-followup BPMN and return run_id immediately. Supply contract and arguments, caller (the initiating agent's request/context capability contract), success_prompt and failure_prompt. Optional resource pins the worker, caller_resource pins the receiver, context carries task/thread correlation, and success_equals matches dotted output fields. The caller receives the selected prompt and output via its normal agent adapter. Retry the same launch with the same request_id."""
    from .process import import_bpmn, start
    prepare(tx, ctx, **args)
    if not tx.get('definitions', NAME):
        path = os.path.join(os.path.dirname(__file__), '..', 'examples', NAME + '.bpmn')
        with open(path, encoding='utf-8') as stream:
            import_bpmn(tx, ctx, NAME, stream.read())
    run = start(tx, ctx, NAME, args)
    return {'run_id': run['id'], 'workflow': NAME, 'revision': run['rev'], 'state': run['state'],
            'caller': args['caller']}


def mismatch(output, rules):
    if rules is None:
        rules = {key: value for key, value in (('exit_code', 0), ('ok', True), ('executed', True),
                                               ('timed_out', False)) if key in output}
    for path, expected in sorted(rules.items()):
        actual = output
        for part in path.split('.'):
            if not isinstance(actual, dict) or part not in actual:
                return 'Missing output field: ' + path
            actual = actual[part]
        if actual != expected or isinstance(actual, bool) != isinstance(expected, bool):
            return 'Output did not match the success condition for ' + path
    return ''


def outcome(run, token, realm, contract, resource, action=None, rules=None, error=''):
    record = {'run_id': run['id'], 'activity': token['at'], 'realm': realm, 'contract': contract,
              'resource': resource, 'ok': False, 'state': 'dispatch_failed', 'error': error[:1200],
              'operation_id': None, 'provider': None, 'provider_state': None, 'result': None,
              'result_inline': False, 'result_ref': None}
    if action is None:
        return record
    output = action.get('result', {})
    problem = mismatch(output, rules) if action['state'] == 'succeeded' else 'Provider reported failure'
    serialized = json.dumps(output, sort_keys=True)
    inline = len(serialized) <= 16000
    record.update(ok=not problem, state='failed' if problem else 'succeeded', error=problem,
                  provider_state=action['state'], provider=action['provider'], resource=action['resource'],
                  operation_id=action['id'], result=output if inline else None, result_inline=inline,
                  result_ref={'operation': 'cap.status', 'realm': realm, 'arguments': {'operation': action['id']}})
    if not inline:
        record['result_preview'] = serialized[:2048]
    return record
