"""Proposal-bound grants and atomic admission at the capability dispatch boundary."""
import copy
import time
from .api import op, ident, Context
from .store import Fault, digest, text
from .decisions import DEFAULT_CHOICES, operator


def validate_proposal(proposal):
    p = copy.deepcopy(proposal)
    required = {'revision', 'action', 'targets', 'affected_systems', 'data_operations',
                'expected_effects', 'consequences', 'scope', 'conditions', 'recovery_actions'}
    if not isinstance(p, dict) or not required <= set(p):
        raise Fault('Concrete proposal requires revision, action, targets, systems, data operations, effects, consequences, scope, conditions and recovery actions')
    if type(p['revision']) is not int or p['revision'] < 1:
        raise Fault('Positive proposal revision required')
    action(p['action'])
    for k in ('targets', 'affected_systems', 'data_operations', 'expected_effects', 'consequences', 'conditions', 'recovery_actions'):
        if not isinstance(p[k], list):
            raise Fault('Proposal ' + k + ' must be a list')
    if not p['targets'] or not p['affected_systems'] or not p['expected_effects']:
        raise Fault('Describe concrete targets, affected systems and effects')
    scope = p['scope']
    if not isinstance(scope, dict) or scope.get('kind') not in ('single', 'group', 'standing'):
        raise Fault('Explicit single, group or standing scope required')
    if scope['kind'] != 'single':
        if not isinstance(scope.get('actions'), list) or not scope['actions']:
            raise Fault('Group and standing permissions require exact allowed action descriptors')
        for descriptor in scope['actions']:
            action(descriptor)
        if p['action'] not in scope['actions']:
            raise Fault('Proposed action must be included in the requested scope')
    if 'expires_at' in p and (isinstance(p['expires_at'], bool) or not isinstance(p['expires_at'], (int, float))):
        raise Fault('Explicit expiration must be a Unix timestamp')
    return p


def action(a):
    if not isinstance(a, dict) or set(a) != {'contract', 'resource', 'inputs'}:
        raise Fault('Action requires exact contract, resource and inputs')
    ident(text(a['contract'], 'contract', 128))
    ident(text(a['resource'], 'resource', 128))
    if not isinstance(a['inputs'], dict):
        raise Fault('Action inputs must be an object')
    return a


def active(tx, grant):
    if grant['state'] != 'active':
        return False
    if grant.get('expires_at') is not None and time.time() >= grant['expires_at']:
        return False
    req = tx.need('authorization_requests', grant['request_id'])
    return req['proposal_digest'] == grant['proposal_digest']


def covering(tx, request_id, proposal):
    for g in tx.rows('authorization_grants'):
        if not active(tx, g) or g['proposal_revision'] != proposal['revision']:
            continue
        # Standing/group coverage is explicitly enumerated, never inferred from prose.
        # The full consequences/conditions/targets are part of the approved identity.
        same = {k: v for k, v in proposal.items() if k != 'action'} == {k: v for k, v in g['proposal'].items() if k != 'action'}
        if not same or proposal['action'] not in g['allowed_actions']:
            continue
        if g['scope'] == 'single' and g['request_id'] != request_id:
            continue
        used = [u for u in tx.rows('authorization_usage') if u['authorization_id'] == g['id']]
        if g['scope'] == 'single' and used:
            continue
        if g['scope'] == 'group' and any(u['action'] == proposal['action'] for u in used):
            continue
        return g


def answer_result(outcome, request_id, authorization_id=''):
    return dict(outcome=outcome, request_id=request_id, authorization_id=authorization_id)


@op('authorization.prepare', 'request_id:str workflow_instance_id:str proposal:obj question:str supporting?:obj')
def prepare(tx, ctx, request_id, workflow_instance_id, proposal, question, supporting=None):
    """Check explicit coverage, otherwise persist a revision-bound question for human.decision."""
    operator(ctx)
    ident(request_id)
    text(workflow_instance_id, 'calling workflow instance ID', 128)
    p = validate_proposal(proposal)
    signature = digest(p)
    old = tx.get('authorization_requests', request_id)
    if old:
        if old['workflow_instance_id'] != workflow_instance_id:
            raise Fault('Request belongs to another workflow', 409)
        if old['proposal_digest'] != signature:
            if old['proposal']['revision'] >= p['revision']:
                raise Fault('Changed proposal requires a new revision', 409)
            for g in tx.rows('authorization_grants'):
                if g['request_id'] == request_id and g['state'] == 'active':
                    g['state'] = 'superseded'
                    tx.put('authorization_grants', g['id'], g)
            from .decisions import close
            for d in tx.rows('decisions'):
                if d['request']['id'] == request_id and d['state'] == 'pending':
                    close(tx, ctx, d, 'superseded')
        elif old['question'] != question or old.get('supporting', {}) != (supporting or {}):
            raise Fault('Question changed without a proposal revision', 409)
    if not old or old['proposal_digest'] != signature:
        history = old.get('history', []) + [dict(proposal=old['proposal'], proposal_digest=old['proposal_digest'])] if old else []
        old = dict(id=request_id, workflow_instance_id=workflow_instance_id, proposal=p,
                   proposal_digest=signature, question=text(question, 'question', 16000),
                   supporting=supporting or {}, history=history, state='pending')
        tx.put('authorization_requests', request_id, old)
    grant = covering(tx, request_id, p)
    if grant:
        return dict(answer_result('approved', request_id, grant['id']), decision_request={})
    if old.get('outcome'):
        outcome = 'authorization_unavailable' if old['outcome'] == 'approved' else old['outcome']
        return dict(answer_result(outcome, request_id), decision_request={})
    choices = copy.deepcopy(DEFAULT_CHOICES)
    if p['scope']['kind'] != 'single':
        choices[0]['label'] = 'Approve ' + ('standing permission' if p['scope']['kind'] == 'standing' else 'defined group')
    request = dict(id=request_id, workflow_instance_id=workflow_instance_id,
                   proposal_revision=p['revision'], question=question, choices=choices,
                   title='Permission requested', supporting=old['supporting'], proposal=p,
                   authorization_request=request_id)
    return dict(answer_result('decision_required', request_id), decision_request=request)


@op('authorization.resolve', 'request_id:str decision:obj')
def resolve(tx, ctx, request_id, decision):
    """A received answer is evidence, never an implicit grant. Preserve conditional text for clarification."""
    operator(ctx)
    req = tx.need('authorization_requests', request_id)
    row = tx.need('decisions', decision.get('decision_id', ''))
    response = row.get('response')
    if (not response or response != decision or row['request'].get('authorization_request') != request_id or
            row['request'].get('proposal') != req['proposal'] or response['request_id'] != request_id or
            response['proposal_revision'] != req['proposal']['revision'] or not response.get('authenticated')):
        raise Fault('Exact authenticated response for the current proposal required', 403)
    key = digest([request_id, req['proposal_digest'], response['id']])
    prior = tx.get('_authorization_resolutions', key)
    if prior:
        return prior
    meaning = response['outcome']
    outcome = {'approve': 'approved', 'reject': 'rejected', 'request_changes': 'changes_requested',
               'defer': 'deferred', 'clarification': 'clarification_required'}.get(meaning, 'clarification_required')
    # Prose conditions/questions cannot silently broaden or narrow the recorded proposal.
    # Re-present a concrete revised proposal before any permission is issued.
    if meaning == 'approve' and response['text'].strip():
        outcome = 'clarification_required'
    authorization_id = ''
    if outcome == 'approved':
        p = req['proposal']
        authorization_id = digest(['authorization', request_id, req['proposal_digest'], response['id']])
        allowed = [p['action']] if p['scope']['kind'] == 'single' else p['scope']['actions']
        grant = dict(id=authorization_id, request_id=request_id, proposal=p,
                     proposal_revision=p['revision'], proposal_digest=req['proposal_digest'],
                     response_id=response['id'], decision_id=row['id'], scope=p['scope']['kind'],
                     allowed_actions=allowed, conditions=p['conditions'], recovery_actions=p['recovery_actions'],
                     state='active', issued_at=time.time())
        if 'expires_at' in p:
            grant['expires_at'] = p['expires_at']
        tx.put('authorization_grants', authorization_id, grant)
    req.update(state='resolved', outcome=outcome, response_id=response['id'], authorization_id=authorization_id)
    tx.put('authorization_requests', request_id, req)
    result = answer_result(outcome, request_id, authorization_id)
    tx.put('_authorization_resolutions', key, result)
    tx.emit('authorization.' + outcome, result)
    return result


@op('authorization.get', 'id:str', False)
def get(tx, ctx, id):
    operator(ctx)
    g = tx.need('authorization_grants', id)
    return dict(g, active=active(tx, g), usage=[u for u in tx.rows('authorization_usage') if u['authorization_id'] == id])


@op('authorization.revoke', 'id:str reason:str')
def revoke(tx, ctx, id, reason):
    operator(ctx)
    g = tx.need('authorization_grants', id)
    g.update(state='revoked', revocation_reason=text(reason, 'revocation reason', 4000), revoked_at=time.time())
    tx.put('authorization_grants', id, g)
    tx.emit('authorization.revoked', dict(id=id, reason=reason))
    return dict(id=id, state='revoked', running_policy='Already claimed work requires separately authorized cancellation or recovery.')


@op('authorization.protect', 'contract:str resource:str')
def protect(tx, ctx, contract, resource):
    """Opt a capability resource into mandatory guards, including direct cap.invoke calls."""
    operator(ctx)
    ident(contract)
    ident(resource)
    row = dict(contract=contract, resource=resource)
    return tx.put('_authorization_protected', digest([contract, resource]), row)


def assert_grant(tx, authorization_id, proposal, descriptor):
    p = validate_proposal(proposal)
    g = tx.need('authorization_grants', authorization_id)
    if not active(tx, g) or g['proposal_revision'] != p['revision']:
        raise Fault('Authorization revoked, expired or superseded', 403)
    if (descriptor != p['action'] or descriptor not in g['allowed_actions'] or
            {k: v for k, v in p.items() if k != 'action'} != {k: v for k, v in g['proposal'].items() if k != 'action'}):
        raise Fault('Action, targets, effects or conditions outside authorized proposal', 403)
    return g, p


def conditions_met(tx, ctx, proposal):
    from .api import OPERATIONS, invoke
    for condition in proposal['conditions']:
        if not isinstance(condition, dict) or not {'description', 'operation', 'arguments', 'expected'} <= set(condition):
            raise Fault('Condition needs an explicit read-only check before dispatch', 403)
        name = condition['operation']
        if name not in OPERATIONS or OPERATIONS[name][2]:
            raise Fault('Authorization condition must use a read-only Biome operation', 403)
        if name.startswith(('authorization.', 'decision.')):
            raise Fault('Condition cannot recurse into authorization or decision state', 403)
        if invoke(tx, ctx, name, condition['arguments']) != condition['expected']:
            raise Fault('Authorization condition is not satisfied: ' + condition['description'], 403)


def reserve(tx, ctx, authorization_id, proposal, operation, descriptor):
    operator(ctx)
    ident(operation)
    old = tx.get('authorization_usage', operation)
    if old:
        if (old['authorization_id'], old['proposal_digest'], old['action']) != (authorization_id, digest(proposal), descriptor):
            raise Fault('Logical operation identity conflict', 409)
        # A retry reads the same action; it never reserves another execution.
        return old
    g, p = assert_grant(tx, authorization_id, proposal, descriptor)
    for used in tx.rows('authorization_usage'):
        if used['authorization_id'] == authorization_id and (g['scope'] == 'single' or
                g['scope'] == 'group' and used['action'] == descriptor):
            raise Fault('Permission already reserved for another logical operation', 409)
    return tx.put('authorization_usage', operation, dict(id=operation, authorization_id=authorization_id,
        request_id=g['request_id'], response_id=g['response_id'], decision_id=g['decision_id'],
        proposal_digest=digest(p), proposal=p, action=descriptor, state='reserved', reserved_at=time.time()))


@op('authorization.dispatch', 'id:str proposal:obj operation:str')
def dispatch(tx, ctx, id, proposal, operation):
    """Atomically reserve scope and submit exactly one matching capability operation."""
    operator(ctx)
    descriptor = action(proposal.get('action'))
    reserve(tx, ctx, id, proposal, operation, descriptor)
    from .providers import submit
    return submit(tx, ctx, descriptor['contract'], descriptor['resource'], descriptor['inputs'], operation)


def on_lease(tx, ctx, lease, inputs, operation, authorization_id, proposal):
    descriptor = dict(contract=lease['contract'], resource=lease['resource'], inputs=inputs)
    reserve(tx, ctx, authorization_id, proposal, operation, descriptor)
    from .providers import dispatch as send
    return send(tx, ctx, lease['id'], inputs, operation)


def guard(tx, operation, contract, resource, inputs):
    usage = tx.get('authorization_usage', operation)
    protected = tx.get('_authorization_protected', digest([contract, resource]))
    if not usage:
        if protected:
            raise Fault('This action requires an authorization guard', 403)
        return
    assert_grant(tx, usage['authorization_id'], usage['proposal'], dict(contract=contract, resource=resource, inputs=inputs))
    conditions_met(tx, Context(None), usage['proposal'])


def observe(tx, operation, state, result=None):
    usage = tx.get('authorization_usage', operation)
    if usage:
        usage.update(state=state, observed_at=time.time())
        if result is not None:
            usage['result'] = result
        tx.put('authorization_usage', operation, usage)
