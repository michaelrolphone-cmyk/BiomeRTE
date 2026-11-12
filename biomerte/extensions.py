"""Composable routine offers, linked plan revisions and deployable provider packages."""
from . import knowledge, providers, process
from .api import op, ident, schema, validate
from .store import Fault, digest, text, uid

PLAN_FIELDS = ('title', 'priority', 'sequence', 'dependencies')
PLAN_PREFIX = 'plan.'


def record_plan(tx, ctx, item):
    key = PLAN_PREFIX + item['id']
    parents = [item['plan_rev']] if item.get('plan_rev') else []
    body = {k: item[k] for k in PLAN_FIELDS}
    body.update(kind=item['kind'], parent=item['parent'])
    r = knowledge.put(tx, ctx, key, body, parents, [item['id']], 'plan')
    item['plan_rev'] = r['rev']
    tx.put('work', item['id'], item)
    return r


@op('plan.apply', 'id:str parents:list values:obj')
def apply_plan(tx, ctx, id, parents, values):
    """Materialize or resolve a replicated plan; acceptance and assignments remain local decisions."""
    from .work import PARENTS, check_deps
    key = PLAN_PREFIX + ident(id)
    doc = tx.need('documents', key)
    if set(parents) != set(doc['heads']):
        raise Fault('Plan heads changed', 409)
    if len(parents) == 1 and not values:
        body = tx.need('revisions', parents[0])['body']
    else:
        body = values
    if set(body) != set(PLAN_FIELDS + ('kind', 'parent')) or body['kind'] not in PARENTS:
        raise Fault('A complete merged plan body is required')
    if not isinstance(body['priority'], int) or not isinstance(body['sequence'], int):
        raise Fault('Invalid plan priority or order')
    text(body['title'])
    if not isinstance(body['dependencies'], list):
        raise Fault('Invalid dependencies')
    check_deps(tx, id, body['dependencies'])
    if body['parent'] and tx.need('work', body['parent'])['kind'] != PARENTS[body['kind']]:
        raise Fault('Invalid parent')
    item = tx.get('work', id)
    if item and (item['kind'] != body['kind'] or item['parent'] != body['parent']):
        raise Fault('Conflicting work hierarchy', 409)
    if not item:
        item = {'id': id, 'criteria': [], 'context': [key], 'state': 'open', 'attempts': [],
                'assigned': '', 'evidence': {}, 'revision': 0}
    if len(parents) > 1 or values:
        item['plan_rev'] = knowledge.put(tx, ctx, key, body, parents, [id], 'plan')['rev']
    else:
        item['plan_rev'] = parents[0]
    item.update(body)
    item['revision'] += 1
    tx.put('work', id, item)
    tx.emit('work.plan_applied', {'id': id})
    return item


@op('routine.expose', 'name:str capability:str version:int')
def expose(tx, ctx, name, capability, version):
    """Make one immutable routine revision discoverable and callable as a capability."""
    d = tx.need('definitions', name)
    c = providers.define(tx, ctx, capability, d['inputs'], d['outputs'], version, 'Routine ' + name)
    rid = 'routine.' + digest([c['id']])[:24]
    return providers.offer(tx, ctx, 'routine', c['id'], rid, configuration={'process': name, 'revision': d['rev']})


@op('agent.progress', 'provider:str operation:str generation:int checkpoint:obj')
def progress(tx, ctx, provider, operation, generation, checkpoint):
    """Persist the executor's intermediate context without completing its assignment."""
    from .lifecycle import heartbeat
    heartbeat(tx, ctx, provider, operation, generation)
    a = tx.need('actions', operation)
    if a['state'] != 'claimed' or (a['provider'], a['generation']) != (provider, generation):
        raise Fault('Assignment is not held by caller', 409)
    a['checkpoint'] = checkpoint
    return tx.put('actions', operation, a)


@op('work.reopen', 'id:str revision:int reason:str')
def reopen(tx, ctx, id, revision, reason):
    item = tx.need('work', id)
    if item['revision'] != revision or item['state'] not in ('review', 'accepted'):
        raise Fault('Expected a terminal attempt or accepted task at this revision', 409)
    item.update(state='open', revision=revision + 1)
    item.setdefault('prior_evidence', []).append(
        {'reason': text(reason), 'evidence': item['evidence']})
    item['evidence'] = {}
    tx.put('work', id, item)
    tx.emit('work.reopened', {'id': id})
    return item


@op('package.stage', 'name:str contract:str artifact:str probes:list')
def stage(tx, ctx, name, contract, artifact, probes):
    """Stage a content-addressed Python adapter and test vectors for explicit host installation."""
    c = tx.need('contracts', contract)
    tx.need('artifacts', artifact)
    if not probes or len(probes) > 16:
        raise Fault('Supply 1–16 probe vectors')
    for p in probes:
        validate(c['inputs'], p['inputs'])
        validate(c['outputs'], p['expected'])
    body = {'name': ident(name), 'contract': contract, 'artifact': artifact, 'probes': probes}
    key = digest(body)
    return tx.put('packages', key, dict(body, id=key))


@op('package.get', 'id:str', False)
def package_get(tx, ctx, id):
    return tx.need('packages', id)


@op('process.migrate', 'run:str expected_rev:str target_rev:str nodes:obj variables?:obj')
def migrate(tx, ctx, run, expected_rev, target_rev, nodes, variables=None):
    """Map tokens at a safe point onto an immutable revision; retain previous revisions and evidence."""
    r = tx.need('runs', run)
    if r['state'] != 'running' or r['rev'] != expected_rev:
        raise Fault('Run changed', 409)
    target = tx.need('process_versions', target_rev)
    if target['name'] != r['name']:
        raise Fault('Target belongs to another process')
    for token in r['tokens']:
        if any(k in token for k in ('action', 'wait', 'child', 'delay_id')) or token['groups'] or token['loops']:
            raise Fault('Finish or reconcile outstanding activities before migration', 409)
        if token['at'] not in nodes or nodes[token['at']] not in target['graph']['nodes']:
            raise Fault('Missing target activity')
    for token in r['tokens']:
        token['at'] = nodes[token['at']]
        token['vars'].update(variables or {})
    r.setdefault('migrations', []).append(
        {'from': expected_rev, 'to': target_rev, 'mapping': nodes, 'by': ctx.actor})
    r['rev'] = target_rev
    tx.put('runs', run, r)
    tx.emit('process.migrated', {'run': run, 'rev': target_rev})
    return r


@op('cap.prepare', 'contract:str resource?:str placement?:obj')
def prepare(tx, ctx, contract, resource='', placement=None):
    """Arrange activation of a configurable offer. Provider confirms before use."""
    resource, placement = providers.selection.normalize(resource, placement)
    offers = [o for o in tx.rows('offers') if o['contract'] == contract and
              providers.selection.matches(o, resource, placement)]
    for off in offers:
        if providers.admitted(tx, off):
            return {'state': 'ready', 'offer': off['id']}
    for off in offers:
        if off['state'] != 'available_after_setup' or off.get('activation_error'):
            continue
        if not providers.admitted(tx, dict(off, state='ready')):
            continue
        if providers.active_on(tx, off['resource']):
            continue
        active = next((r for r in tx.rows('activations') if r['resource'] == off['resource'] and
                       r['state'] == 'pending'), None)
        if active:
            return active
        a = {'id': uid(), 'offer': off['id'], 'provider': off['provider'],
             'resource': off['resource'], 'state': 'pending'}
        tx.put('activations', a['id'], a)
        tx.emit('offer.activation_requested', a)
        return a
    return {'state': 'unmet' if offers else 'missing', 'contract': contract, 'resource': resource}


@op('cap.configured', 'provider:str activation:str success:bool evidence:obj')
def configured(tx, ctx, provider, activation, success, evidence):
    providers.owner(ctx, provider)
    a = tx.need('activations', activation)
    if a['provider'] != provider or a['state'] != 'pending':
        raise Fault('Unexpected activation', 409)
    a.update(state='active' if success else 'failed', evidence=evidence)
    off = tx.need('offers', a['offer'])
    if success:
        off['state'] = 'ready'
    else:
        off['activation_error'] = {'activation': activation, 'evidence': evidence}
    tx.put('offers', off['id'], off)
    tx.put('activations', activation, a)
    tx.emit('offer.configured', {'id': activation, 'state': a['state']})
    return a


def demand(tx, ctx, contract, resource, requester, activity):
    """Create one actionable construction task for a missing contract/resource pair."""
    from .work import create
    key = 'cap-need-' + digest([contract, resource])[:24]
    need = tx.get('demands', key)
    reference = {'run': requester, 'activity': activity}
    if need is None:
        create(tx, ctx, 'task', 'Supply ' + contract, id=key,
               criteria=['verified capability operation'], context=[reference])
        need = {'id': key, 'contract': contract, 'resource': resource, 'requests': []}
    if reference not in need['requests']:
        need['requests'].append(reference)
        tx.put('demands', key, need)
        tx.emit('capability.needed', {'task': key, 'run': requester, 'contract': contract})
    return need
