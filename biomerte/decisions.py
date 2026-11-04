"""Durable human decisions. Only enrolled, signed mobile interactions answer them."""
import base64
import copy
import hmac
import json
import time
from .api import op, Context, ident
from .store import Fault, digest, text, uid
from .identity import secret, fingerprint, verify


DEFAULT_CHOICES = [dict(id=k, label=v, meaning=k) for k, v in (
    ('approve', 'Approve'), ('reject', 'Reject'),
    ('request_changes', 'Request changes'), ('defer', 'Defer'))]


def operator(ctx):
    if ctx.permissions is not None:
        raise Fault('Decision administration requires owner access', 403)


def seed(tx, ctx):
    from . import providers
    if not tx.get('contracts', 'human.decision:1'):
        providers.define(tx, ctx, 'human.decision', {'request': 'obj'}, {'decision': 'obj'},
                         description='Authenticated multiple-choice decision with optional original textual reply.')
        providers.offer(tx, ctx, 'builtin', 'human.decision:1', 'human.decisions', capacity=0,
                        usage={'before': 'Supply a stable request.id, workflow_instance_id, proposal_revision, question and choices [{id,label,meaning}]. Text replies are preserved. This decision alone is not an authorization. Use the reusable biome-authorization BPMN subprocess for permission.',
                               'example_inputs': {'request': {'id': 'example', 'workflow_instance_id': 'workflow-1',
                                'proposal_revision': 1, 'question': 'Continue?', 'choices': DEFAULT_CHOICES}}})


def create(tx, ctx, request, action=None):
    operator(ctx)
    r = copy.deepcopy(request)
    if len(json.dumps(r).encode('utf-8')) > 60000:
        raise Fault('Question exceeds the mobile transport budget; use supporting references')
    key = ident(text(r.get('id'), 'decision request ID', 128))
    text(r.get('workflow_instance_id'), 'calling workflow instance ID', 128)
    revision = r.get('proposal_revision')
    if type(revision) is not int or revision < 1:
        raise Fault('Positive proposal revision required')
    text(r.get('question'), 'question', 16000)
    choices = r.get('choices', DEFAULT_CHOICES)
    if not isinstance(choices, list) or not choices:
        raise Fault('Decision choices required')
    ids = set()
    for choice in choices:
        if not isinstance(choice, dict):
            raise Fault('Invalid choice')
        ident(text(choice.get('id'), 'choice ID', 128))
        text(choice.get('label'), 'choice label', 1000)
        text(choice.get('meaning'), 'explicit choice meaning', 128)
        if choice['id'] in ids:
            raise Fault('Duplicate choice')
        ids.add(choice['id'])
    r['choices'] = choices
    signature = digest(r)
    record_id = digest([key, revision])
    old = tx.get('decisions', record_id)
    if old:
        if old['digest'] != signature:
            raise Fault('Decision revision has different content', 409)
        if action and action not in old['actions']:
            old['actions'].append(action)
            tx.put('decisions', record_id, old)
        return old
    for prior in tx.rows('decisions'):
        if prior['request']['id'] == key:
            if prior['request']['proposal_revision'] >= revision:
                raise Fault('Obsolete decision proposal', 409)
            if prior['state'] == 'pending':
                close(tx, ctx, prior, 'superseded')
    row = dict(id=record_id, request=r, digest=signature, state='pending',
               actions=[action] if action else [], created_at=time.time(), responses=[])
    tx.put('decisions', record_id, row)
    tx.emit('human.decision.requested', {'id': record_id, 'request_id': key, 'proposal_revision': revision})
    return row


def close(tx, ctx, row, state):
    row['state'] = state
    tx.put('decisions', row['id'], row)
    for key in row['actions']:
        a = tx.get('actions', key)
        if a and a['state'] == 'claimed':
            a.update(state='succeeded', result={'decision': row.get('response', {
                'outcome': state, 'request_id': row['request']['id'],
                'proposal_revision': row['request']['proposal_revision'], 'decision_id': row['id']})})
            tx.put('actions', key, a)
            from .providers import finish_submission
            finish_submission(tx, ctx, key)
    tx.emit('human.decision.' + state, {'id': row['id']})


def start_action(tx, ctx, action):
    row = create(tx, ctx, action['inputs']['request'], action['id'])
    action.update(state='claimed', decision_id=row['id'])
    if row['state'] != 'pending':
        action.update(state='succeeded', result={'decision': row.get('response', {
            'outcome': row['state'], 'decision_id': row['id'],
            'request_id': row['request']['id'], 'proposal_revision': row['request']['proposal_revision']})})


@op('decision.get', 'id:str', False)
def get(tx, ctx, id):
    operator(ctx)
    return tx.need('decisions', id)


@op('decision.list', write=False)
def listing(tx, ctx):
    operator(ctx)
    return tx.rows('decisions')


@op('decision.withdraw', 'id:str reason:str')
def withdraw(tx, ctx, id, reason):
    operator(ctx)
    row = tx.need('decisions', id)
    if row['state'] != 'pending':
        raise Fault('Decision already settled', 409)
    row['withdraw_reason'] = text(reason, 'reason', 4000)
    close(tx, ctx, row, 'withdrawn')
    return row


@op('decision.pair', 'label:str')
def pair(tx, ctx, label):
    operator(ctx)
    code, key = secret(), uid()
    tx.put('_decision_pairings', key, dict(id=key, code_hash=digest(code), label=text(label), state='open'))
    return dict(format='biome-decisions-pairing/1', site=ctx.app.site, pair_id=key, code=code,
                origin='https://biomerte-controller.cantikash.chatgpt.site')


@op('decision.device.revoke', 'device:str reason:str')
def revoke_device(tx, ctx, device, reason):
    operator(ctx)
    row = tx.need('_decision_devices', device)
    row.update(enabled=False, revocation_reason=text(reason, 'reason', 4000))
    return tx.put('_decision_devices', device, row)


@op('decision.pair.revoke', 'pair_id:str')
def revoke_pairing(tx, ctx, pair_id):
    operator(ctx)
    row = tx.need('_decision_pairings', pair_id)
    if row['state'] == 'used':
        raise Fault('Pairing already redeemed; revoke the enrolled device instead', 409)
    row['state'] = 'revoked'
    tx.put('_decision_pairings', pair_id, row)
    return dict(id=pair_id, state='revoked')


@op('decision.mobile', 'envelope:obj')
def mobile(tx, ctx, envelope):
    """Narrow gateway ingress: never accepts an owner-asserted answer without a phone signature."""
    e = copy.deepcopy(envelope)
    if e.get('command') == 'enroll':
        if set(e) != {'command', 'pair_id', 'code', 'public_key', 'installation'}:
            raise Fault('Invalid enrollment')
        p = tx.need('_decision_pairings', e['pair_id'])
        if p['state'] == 'revoked':
            raise Fault('Pairing revoked', 403)
        who = fingerprint(e['public_key'])
        if not hmac.compare_digest(p['code_hash'], digest(e['code'])):
            raise Fault('Invalid pairing code', 403)
        if p['state'] == 'used':
            if p['device'] != who or p['installation'] != e['installation']:
                raise Fault('Pairing already used', 409)
            return dict(device=who, site=ctx.app.site)
        ident(text(e['installation'], 'installation', 128))
        tx.put('_decision_devices', who, dict(id=who, key=e['public_key'], label=p['label'],
                                             installation=e['installation'], enabled=True))
        p.update(state='used', device=who, installation=e['installation'])
        tx.put('_decision_pairings', p['id'], p)
        return dict(device=who, site=ctx.app.site)
    if set(e) != {'device', 'site', 'id', 'command', 'payload', 'signature'}:
        raise Fault('Invalid signed decision envelope')
    signature = e.pop('signature')
    if e['site'] != ctx.app.site:
        raise Fault('Different Biome site', 403)
    dev = tx.need('_decision_devices', e['device'])
    if not dev['enabled']:
        raise Fault('Decision device revoked', 403)
    ident(e['id'])
    verify(dev['key'], signature, e)
    try:
        args = json.loads(base64.b64decode(e['payload'], validate=True).decode('utf-8'))
    except (ValueError, UnicodeError):
        raise Fault('Invalid mobile payload')
    if not isinstance(args, dict):
        raise Fault('Invalid mobile arguments')
    if e['command'] == 'inbox':
        cursor = args.get('cursor', '')
        if not isinstance(cursor, str):
            raise Fault('Invalid inbox cursor')
        rows = [r for r in tx.rows('decisions') if r['state'] == 'pending' and r['id'] > cursor]
        page, size = [], 0
        for row in rows:
            needed = len(json.dumps(row).encode('utf-8'))
            if page and size + needed > 128000:
                break
            page.append(row)
            size += needed
        return dict(decisions=page, next_cursor=page[-1]['id'] if len(page) < len(rows) else '',
                    site=ctx.app.site, observed_at=time.time())
    if e['command'] != 'respond':
        raise Fault('Unsupported phone command', 403)
    receipt_id = digest([dev['id'], e['id']])
    prior = tx.get('_decision_responses', receipt_id)
    if prior:
        if prior['envelope_digest'] != digest(e):
            raise Fault('Response identity reused', 409)
        return prior['response']
    if set(args) != {'decision_id', 'digest', 'proposal_revision', 'choice', 'text'}:
        raise Fault('Exact decision, revision, choice and original text required')
    row = tx.need('decisions', args['decision_id'])
    if row['digest'] != args['digest'] or row['request']['proposal_revision'] != args['proposal_revision']:
        raise Fault('Question changed; review the current proposal', 409)
    if row['state'] != 'pending':
        raise Fault('Decision already settled', 409)
    if not isinstance(args['text'], str) or len(args['text'].encode('utf-8')) > 16384:
        raise Fault('Invalid textual reply')
    choices = {x['id']: x for x in row['request']['choices']}
    if args['choice'] and args['choice'] not in choices:
        raise Fault('Unknown choice')
    if not args['choice'] and not args['text'].strip():
        raise Fault('A choice or textual reply is required')
    response = dict(id=receipt_id, decision_id=row['id'], request_id=row['request']['id'],
                    proposal_revision=args['proposal_revision'], question_digest=row['digest'],
                    choice=args['choice'], text=args['text'],
                    outcome=choices[args['choice']]['meaning'] if args['choice'] else 'clarification',
                    device=dev['id'], authenticated=True, received_at=time.time(),
                    signed_envelope=dict(e, signature=signature))
    row['responses'].append(response)
    row['response'] = response
    close(tx, ctx, row, 'answered')
    tx.put('_decision_responses', receipt_id, dict(envelope_digest=digest(e), response=response))
    return response


@op('decision.mobile.inbox', 'envelope:obj', False)
def mobile_inbox(tx, ctx, envelope):
    if envelope.get('command') != 'inbox':
        raise Fault('This ingress only reads the signed phone inbox', 403)
    return mobile(tx, ctx, envelope)
