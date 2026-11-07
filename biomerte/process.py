"""Versioned executable processes. Routines and BPMN imports target the same graph."""
import copy
import json
import operator
import time
from .api import op, ident, schema, validate, invoke
from .store import Fault, uid, digest, text
from . import providers, continuation
from .work import ready

TYPES = set('start end fail set choose fork join service call human message delegate timer each select'.split())
INTERNAL = set(
    'knowledge.put knowledge.get knowledge.search bus.send bus.publish artifact.put artifact.get work.list continuation.prepare authorization.prepare authorization.resolve'.split())


def val(expr, variables, depth=0):
    if depth > 32:
        raise Fault('Expression too deep')
    ev = lambda x: val(x, variables, depth + 1)
    if isinstance(expr, list):
        return [ev(e) for e in expr]
    if not isinstance(expr, dict):
        return expr
    if set(expr) == {'$'}:
        value = variables
        for key in expr['$'].split('.'):
            if isinstance(value, list) and key.isdigit():
                value = value[int(key)]
            elif isinstance(value, dict) and key in value:
                value = value[key]
            else:
                raise Fault('Unknown variable: ' + expr['$'])
        return copy.deepcopy(value)
    if set(expr) == {'op', 'args'}:
        args = [ev(e) for e in expr['args']]
        ops = {'eq': operator.eq, 'ne': operator.ne, 'gt': operator.gt, 'ge': operator.ge,
               'lt': operator.lt, 'le': operator.le, 'add': operator.add, 'sub': operator.sub,
               'and': lambda a, b: bool(a and b), 'or': lambda a, b: bool(a or b),
               'not': operator.not_, 'len': len}
        if expr['op'] not in ops:
            raise Fault('Unsupported expression')
        return ops[expr['op']](*args)
    return {key: ev(v) for key, v in expr.items()}


def failed_address(binding, variables):
    # A missing dynamic binding still takes the BPMN error branch.
    try:
        value = val(binding, variables)
        return value if isinstance(value, str) else None
    except (Fault, IndexError, TypeError, AttributeError):
        return None


def targets(n):
    k = n['type']
    if k in ('end', 'fail'):
        return []
    if k == 'choose':
        return [b['to'] for b in n['cases']] + [n['default']]
    if k == 'fork':
        return n['branches']
    return [n['next']] + ([n['body']] if k == 'each' else [])


def validate_graph(g):
    nodes = g['nodes']
    if not isinstance(nodes, dict) or len(nodes) > 256 or not nodes:
        raise Fault('A process needs 1–256 nodes')
    schema(g.get('inputs', {}))
    schema(g.get('outputs', {}))
    seen = set()
    pending = [g['start']]
    while pending:
        key = pending.pop()
        if key in seen:
            continue
        ident(key)
        if key not in nodes or nodes[key].get('type') not in TYPES:
            raise Fault('Unknown node or type: ' + key)
        seen.add(key)
        n = nodes[key]
        if n['type'] == 'fork' and (len(n['branches']) < 2 or len(set(n['branches'])) != len(n['branches'])):
            raise Fault('Parallel split needs distinct branches')
        if n['type'] == 'service' and (bool(n.get('contract')) == bool(n.get('operation')) or
                                       n.get('operation') and n['operation'] not in INTERNAL):
            raise Fault('Service needs one contract or supported internal operation')
        if n.get('capture_outcome') or n.get('lease_scope') or 'success_equals' in n:
            if n['type'] != 'service' or not n.get('contract') or n.get('lease_scope') != 'activity':
                raise Fault('Outcome capture and activity leases require a capability service task')
            if 'success_equals' in n and not n.get('capture_outcome'):
                raise Fault('A success condition requires outcome capture')
        if n['type'] == 'delegate':
            if not isinstance(n.get('to'), str) or 'request' not in n:
                raise Fault('A delegate node needs a supported receiver and a request')
            ident(n['to'])
            schema(n.get('outputs', {}))
        if n['type'] == 'call':
            ident(n['process'])
        pending.extend(targets(n))
        if 'on_error' in n:
            pending.append(n['on_error'])
    if seen != set(nodes) or not any(n['type'] in ('end', 'fail') for n in nodes.values()):
        raise Fault('Unreachable activity or missing end event')


@op('process.publish', 'name:str graph:obj')
def publish(tx, ctx, name, graph):
    """Publish an immutable graph revision. Existing runs retain their revision and call graph."""
    ident(name)
    validate_graph(graph)
    calls = {}
    for n in graph['nodes'].values():
        if n['type'] == 'call':
            child = tx.need('definitions', n['process'])
            calls[n['process']] = child['rev']
    rev = digest([name, graph, calls])
    rec = {'id': rev, 'name': name, 'graph': graph, 'calls': calls}
    tx.put('process_versions', rev, rec)
    tx.put('definitions', name, {'id': name, 'rev': rev, 'inputs': graph.get('inputs', {}),
                                 'outputs': graph.get('outputs', {})})
    tx.emit('process.published', {'name': name, 'rev': rev})
    return {'name': name, 'rev': rev}


def routine_graph(steps, inputs, outputs, result):
    """Compile sequential, branching and looping steps into a process graph."""
    nodes = {'end': {'type': 'end', 'result': result}}

    def add(n):
        key = 'n' + str(len(nodes))
        nodes[key] = n
        return key

    def sequence(items, after, handler=None):
        for item in reversed(items):
            item = dict(item)
            if 'try' in item:
                recovery = sequence(item['catch'], after, handler)
                after = sequence(item['try'], after, recovery)
                continue
            if 'if' in item:
                yes = sequence(item.get('then', []), after, handler)
                no = sequence(item.get('else', []), after, handler)
                after = add({'type': 'choose', 'cases': [
                            {'when': item['if'], 'to': yes}], 'default': no})
            elif 'each' in item:
                key = add({'type': 'each', 'items': item['each'], 'as': item['as'], 'next': after})
                nodes[key]['body'] = sequence(item['do'], key, handler)
                after = key
            else:
                item['next'] = after
                after = add(item)
            if handler:
                nodes[after]['on_error'] = handler
        return after
    start = sequence(steps, 'end')
    if start != 'end' and not any('end' in targets(n) or n.get('on_error') == 'end' for n in nodes.values()):
        del nodes['end']  # A routine may explicitly terminate with failure on every path.
    return {'nodes': nodes, 'start': start, 'inputs': inputs, 'outputs': outputs}


@op('routine.publish', 'name:str steps:list inputs:obj outputs:obj result:obj')
def routine(tx, ctx, name, steps, inputs, outputs, result):
    g = routine_graph(steps, inputs, outputs, result)
    info = publish(tx, ctx, name, g)
    tx.put('recipes', info['rev'], {'steps': steps, 'result': result})
    return info


def token(node, variables, groups=None):
    return {'id': uid(), 'at': node, 'vars': variables, 'groups': groups or [], 'loops': {}}


def new_run(tx, name, inputs, task='', parent='', pin='', depth=0):
    if depth > 24:
        raise Fault('Call depth limit')
    rev = pin or tx.need('definitions', name)['rev']
    graph = tx.need('process_versions', rev)['graph']
    validate(schema(graph.get('inputs', {})), inputs)
    run = {'id': uid(), 'name': name, 'rev': rev, 'task': task, 'parent': parent, 'depth': depth,
           'state': 'running', 'tokens': [token(graph['start'], {'input': inputs})],
           'steps': 0, 'result': {}, 'trace': [], 'joins': {}, 'children': []}
    tx.put('runs', run['id'], run)
    return run


@op('process.start', 'name:str inputs:obj task?:str')
def start(tx, ctx, name, inputs, task=''):
    """Start a durable process; poll process.get or subscribe for completion."""
    if task:
        item = tx.need('work', task)
        if not ready(tx, item):
            raise Fault('Task is not ready', 409)
    r = new_run(tx, name, inputs, task)
    if task:
        item['attempts'].append(r['id'])
        item.update(state='running', revision=item['revision'] + 1)
        tx.put('work', task, item)
    tx.emit('process.started', {'run': r['id'], 'name': name})
    return r


@op('process.get', 'run:str', False)
def get(tx, ctx, run):
    return tx.need('runs', run)


@op('process.list', write=False)
def listing(tx, ctx):
    return {'definitions': tx.rows('definitions'), 'runs': [
        {k: r[k] for k in ('id', 'name', 'task', 'parent', 'state', 'steps', 'rev')} for r in tx.rows('runs')]}


def finish(tx, run, state, result):
    for token in run['tokens']:
        held = token.get('activity_lease') or token.get('queued_lease')
        if held:
            waiting = tx.get('leases', held)
            if waiting and waiting['state'] != 'released':
                providers.release(tx, waiting)
    run.update(state=state, result=result)
    if run['parent'].startswith('action:'):
        a = tx.need('actions', run['parent'][7:])
        a.update(state='succeeded' if state == 'completed' else 'failed', result=result)
        tx.put('actions', a['id'], a)
        tx.emit('operation.' + a['state'], {'id': a['id']})
    if not run['parent'] and run['task']:
        item = tx.need('work', run['task'])
        if item['state'] == 'running':
            item.update(state='review' if state == 'completed' else 'open',
                        revision=item['revision'] + 1)
            tx.put('work', item['id'], item)
    if not run['task']:
        providers.release_task(tx, run['id'])
    tx.emit('process.' + state, {'run': run['id'], 'process': run['name']})


def advance(tx, ctx, r, t, n, version):
    """Advance one token. Return False for an unchanged durable wait."""
    kind, variables = n['type'], t['vars']
    ctx.workflow = {'run_id': r['id'], 'name': r['name'], 'revision': r['rev'], 'node': t['at'], 'token_id': t['id']}
    ev = lambda value: val(value, variables)
    dest = n.get('next')
    result = None
    if kind == 'end':
        if t['groups']:
            raise Fault('Parallel branch exited without its join')
        result = ev(n.get('result', {}))
        validate(schema(version['graph'].get('outputs', {})), result)
        r['tokens'].remove(t)
        if not r['tokens']:
            finish(tx, r, 'completed', result)
        return True
    if kind == 'fail':
        raise Fault(str(ev(n.get('reason', 'Process failed'))))
    if kind == 'set':
        variables.update(ev(n['values']))
    elif kind == 'choose':
        dest = next((p['to'] for p in n['cases'] if ev(p['when'])), n['default'])
    elif kind == 'each':
        loop = t['loops'].get(t['at'])
        if loop is None:
            items = ev(n['items'])
            if not isinstance(items, list) or len(items) > 10000:
                raise Fault('Invalid iteration input')
            loop = {'items': items, 'index': 0}
            t['loops'][t['at']] = loop
        if loop['index'] < len(loop['items']):
            variables[n['as']] = loop['items'][loop['index']]
            loop['index'] += 1
            dest = n['body']
        else:
            del t['loops'][t['at']]
    elif kind == 'fork':
        group = uid()
        r['joins'][group] = {'expected': len(n['branches']), 'arrived': [
        ], 'base': copy.deepcopy(variables)}
        r['tokens'].remove(t)
        for branch in n['branches']:
            child = copy.deepcopy(t)
            child.update(id=uid(), at=branch, groups=t['groups'] + [group])
            r['tokens'].append(child)
        return True
    elif kind == 'join':
        if not t['groups']:
            raise Fault('No matching parallel split')
        group = t['groups'].pop()
        join = r['joins'][group]
        if join.setdefault('node', t['at']) != t['at']:
            raise Fault('Parallel branches reached different joins')
        join['arrived'].append(variables)
        if len(join['arrived']) < join['expected']:
            r['tokens'].remove(t)
            return True
        merged = {}
        for branch in join['arrived']:
            for k, v in branch.items():
                if k in join['base'] and v == join['base'][k]:
                    continue
                if k in merged and merged[k] != v:
                    raise Fault('Conflicting parallel outputs: ' + k)
                merged[k] = v
        variables = copy.deepcopy(join['base'])
        variables.update(merged)
        t['vars'] = variables
        del r['joins'][group]
    elif kind == 'select':
        candidates = [x for x in tx.rows('work') if x['kind'] == 'task' and ready(tx, x)]
        if not candidates:
            return False
        item = sorted(candidates, key=lambda x: (-x['priority'], x['sequence'], x['id']))[0]
        result = start(tx, ctx, n['process'], ev(n.get('args', {})), item['id'])['id']
    elif kind == 'call':
        if 'child' not in t:
            child = new_run(tx, n['process'], ev(n.get('args', {})), r['task'], r['id'],
                            version['calls'][n['process']], r['depth'] + 1)
            t['child'] = child['id']
            r['children'].append(child['id'])
            return True
        child = tx.need('runs', t['child'])
        if child['state'] == 'running':
            return False
        del t['child']
        if child['state'] != 'completed':
            raise Fault('Subroutine %s %s: %s' % (child['name'], child['state'], child['result'].get('error', 'no successful result')))
        result = child['result']
    elif kind in ('human', 'message', 'delegate'):
        if 'wait' not in t:
            wait_id = uid()
            w = {'id': wait_id, 'run': r['id'], 'kind': kind, 'name': n.get('name', t['at']),
                 'question': ev(n.get('question', '')), 'answer_schema': n.get('outputs', {}), 'state': 'open'}
            if kind == 'delegate':
                from datetime import datetime, timezone
                request = text(ev(n['request']), 'delegated request', 8000)
                context = ev(n.get('context', {}))
                if not isinstance(context, dict) or len(json.dumps(context).encode('utf-8')) > 32000:
                    raise Fault('Delegation context must be an object of at most 32 kB')
                w.update(to=n['to'], question=request, context=context, workflow=r['name'],
                         activity=t['at'], task=r['task'])
                seq = tx.get('_meta', 'delegation_sequence', 0) + 1
                data = {'delegation_id': wait_id, 'run_id': r['id'], 'workflow': r['name'],
                        'activity': t['at'], 'realm': ctx.scope}
                tx.put('delegation_events', '%020d' % seq, {'seq': seq, 'target': n['to'], 'data': data,
                       'timestamp': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')})
                tx.put('_meta', 'delegation_sequence', seq)
                tx.emit('workflow.delegation.requested', dict(data, target=n['to']))
            t['wait'] = wait_id
            tx.put('waits', wait_id, w)
            tx.emit('wait.open', {'id': wait_id, 'run': r['id'], 'kind': kind})
            return True
        wait = tx.need('waits', t['wait'])
        if wait['state'] == 'failed':
            raise Fault(wait['error'])
        if wait['state'] != 'answered':
            return False
        result = wait['answer']
        del t['wait']
    elif kind == 'timer':
        if 'delay_id' not in t:
            seconds = ev(n['seconds'])
            if not isinstance(seconds, (int, float)) or seconds < 0:
                raise Fault('Invalid delay')
            t['delay_id'], t['seconds'] = uid(), seconds
            t['deadline'] = time.time() + seconds
            ctx.app.timer_anchors[t['delay_id']] = time.monotonic()
            return True
        if 'deadline' not in t:
            # Legacy waits retain the elapsed time observed by their running owner.
            anchor = ctx.app.timer_anchors.get(t['delay_id'], time.monotonic())
            t['deadline'] = time.time() + max(0, t['seconds'] - (time.monotonic() - anchor))
            return True
        if time.time() < t['deadline']:
            return False
        ctx.app.timer_anchors.pop(t.pop('delay_id'), None)
        del t['seconds']
        del t['deadline']
    elif kind == 'service':
        inputs = ev(n.get('args', {}))
        if 'operation' in n:
            result = invoke(tx, ctx, n['operation'], inputs)
            if n['operation'].startswith('assistant.') and isinstance(result, dict) and 'frame' in result:
                from .assistant_workspaces import hold_unresolved
                result = hold_unresolved(result)
            workspace_return = n['operation'] in ('assistant.child', 'assistant.finish', 'assistant.fault') and result.get('frame', {}).get('workspace')
            if (n['operation'] in ('assistant.stage', 'assistant.before', 'assistant.wait') or workspace_return) and result.get('disposition') == 'pending' and not result.get('result', {}).get('result_code', '').startswith('independent_'):
                # Keep the exact token/call stack. No inference lease is held by this wait.
                variables['frame'] = result['frame']
                t['assistant_wait'] = result['continuation']
                tx.put('runs', r['id'], r)
                return False
            t.pop('assistant_wait', None)
        else:
            contract = ev(n['contract'])
            resource = ev(n.get('resource', ''))
            scoped = n.get('lease_scope') == 'activity'
            if 'action' not in t:
                if scoped:
                    if not isinstance(contract, str) or not isinstance(resource, str):
                        raise Fault('Invalid capability address')
                    definition = tx.need('contracts', ident(contract))
                    validate(definition['inputs'], inputs)
                holder = digest([r['id'], t['id'], t['at']]) if scoped else (r['task'] or r['id'])
                try:
                    lease = providers.acquire(tx, ctx, holder, contract, resource)
                    if lease['state'] == 'queued':
                        t['queued_lease'] = lease['id']
                        raise Fault('Waiting for queued lease ' + lease['id'], 409)
                except Fault as e:
                    if e.status in (404, 409):
                        from .extensions import prepare, demand
                        status = prepare(tx, ctx, contract, resource)
                        waiting = str(e)
                        if status['state'] == 'missing':
                            need = demand(tx, ctx, contract, resource, r['id'], t['at'])
                            waiting = 'Waiting for capability work ' + need['id']
                        if t.get('blocked') == waiting:
                            return False
                        t['blocked'] = waiting
                        return True
                    raise
                t.pop('queued_lease', None)
                if n.get('authorization'):
                    from .authorization import on_lease
                    auth = ev(n['authorization'])
                    a = on_lease(tx, ctx, lease, inputs, auth['operation'], auth['id'], auth['proposal'])
                else:
                    a = providers.dispatch(tx, ctx, lease['id'], inputs, uid())
                t['action'] = a['id']
                if scoped:
                    t['activity_lease'] = lease['id']
                t.pop('blocked', None)
                return True
            a = tx.need('actions', t['action'])
            if a['state'] in ('queued', 'claimed', 'uncertain'):
                return False
            if n.get('capture_outcome'):
                result = continuation.outcome(r, t, ctx.scope, contract, resource, a, ev(n.get('success_equals')))
            elif a['state'] == 'succeeded':
                result = a['result']
            else:
                raise Fault('Provider failed: ' + str(a.get('result')))
            if scoped:
                providers.release(tx, tx.need('leases', t.pop('activity_lease')))
            r['trace'].append({'activity': t['at'], 'contract': a['contract'], 'inputs': inputs,
                               'operation': a['id'], 'resource': a['resource'], 'result': result})
            del t['action']
    if 'save' in n:
        variables[n['save']] = result
    t['at'] = dest
    return True


def assistant_live_variables(nodes):
    """Conservative backwards liveness for compiler scratch variables."""
    def reads(value):
        if isinstance(value, dict):
            if set(value) == {'$'} and isinstance(value['$'], str):
                return {value['$'].split('.')[0]}
            return set().union(*(reads(v) for v in value.values()))
        if isinstance(value, list):
            return set().union(*(reads(v) for v in value))
        return set()
    live = {key: set() for key in nodes}
    changed = True
    while changed:
        changed = False
        for key, node in nodes.items():
            used = reads(node)
            successors = targets(node)
            killed = {node.get('save')} | (set(node.get('values', {})) if node['type'] == 'set' else set())
            for successor in successors:
                if successor != node.get('on_error'):
                    used |= live.get(successor, set()) - killed
            used |= live.get(node.get('on_error'), set())
            if not used <= live[key]:
                live[key] |= used
                changed = True
    return live


def compact_assistant_token(run, token, live):
    if not run['name'].startswith('assistant.'):
        return False
    removed = False
    for key in list(token['vars']):
        if key in token['vars'] and key not in live[token['at']]:
            del token['vars'][key]
            removed = True
    return removed


def _compact_assistant_temporaries(run, version):
    """Keep the recovery helper available for persisted-state maintenance."""
    if not run.get('name', '').startswith('assistant.'):
        return False
    live = assistant_live_variables(version['graph']['nodes'])
    removed = False
    for token in run['tokens']:
        removed = compact_assistant_token(run, token, live) or removed
    return removed


def tick(tx, ctx, budget=64):
    from .api import events_after
    budget = max(1, min(256, budget))
    providers.promote(tx, ctx)
    moves = 0
    for sub in tx.rows('subscriptions'):
        if sub['process']:
            # Starting and acknowledging are atomic. An event filter prevents accidental self-triggers.
            for e in events_after(tx, sub['cursor'], sub['topic'], min(8, budget - moves)):
                new_run(tx, sub['process'], {'event': e})
                sub['cursor'] = e['seq']
                tx.put('subscriptions', sub['id'], sub)
                moves += 1
                if moves == budget:
                    return moves
    runs = tx.matching('runs', 'state', ('running',))
    cursor = tx.get('_meta', 'last_run', '')
    runs.sort(key=lambda r: (r['id'] <= cursor, r['id']))
    for r in runs:
        version = tx.need('process_versions', r['rev'])
        live = assistant_live_variables(version['graph']['nodes']) if r['name'].startswith('assistant.') else None
        for t in list(r['tokens']):
            if r['state'] != 'running' or moves >= budget:
                break
            n = version['graph']['nodes'][t['at']]
            compacted = live is not None and compact_assistant_token(r, t, live)
            if compacted:
                tx.put('runs', r['id'], r)
            # Roll back partial effects and token mutations on a business error.
            backup = copy.deepcopy(r)
            change_backup = dict(tx.changes)
            tx.db.execute('SAVEPOINT activity')
            try:
                if r['steps'] >= 100000:
                    raise Fault('Step budget exhausted')
                changed = advance(tx, ctx, r, t, n, version) or compacted
            except (Fault, ValueError, KeyError, IndexError, TypeError) as exc:
                tx.db.execute('ROLLBACK TO activity')
                tx.changes = change_backup
                r = backup
                failed = next(v for v in r['tokens'] if v['id'] == t['id'])
                if n.get('lease_scope') == 'activity':
                    held = failed.pop('activity_lease', None) or failed.pop('queued_lease', None)
                    if held:
                        providers.release(tx, tx.need('leases', held))
                if n.get('capture_outcome') and 'action' not in failed:
                    if 'save' in n:
                        failed['vars'][n['save']] = continuation.outcome(
                            r, failed, ctx.scope, failed_address(n['contract'], failed['vars']),
                            failed_address(n.get('resource', ''), failed['vars']), error=str(exc))
                    failed.pop('blocked', None)
                    failed['at'] = n['next']
                elif 'on_error' in n:
                    failed.update(at=n['on_error'])
                    for pending in ('action', 'child', 'wait', 'due', 'blocked'):
                        failed.pop(pending, None)
                    failed['vars']['error'] = str(exc)
                else:
                    finish(tx, r, 'failed', {'error': str(exc), 'activity': failed['at']})
                changed = True
                tx.db.execute('RELEASE activity')
                r['steps'] += 1
                tx.put('runs', r['id'], r)
                tx.put('_meta', 'last_run', r['id'])
                moves += 1
                break
            tx.db.execute('RELEASE activity')
            if live is not None and t in r['tokens'] and compact_assistant_token(r, t, live):
                tx.put('runs', r['id'], r)
            if changed:
                r['steps'] += 1
                tx.put('runs', r['id'], r)
                tx.put('_meta', 'last_run', r['id'])
                moves += 1
        if moves >= budget:
            break
    return moves


@op('process.tick', 'budget?:int')
def pump(tx, ctx, budget=64):
    return {'advanced': tick(tx, ctx, budget)}


@op('process.answer', 'wait:str answer:obj')
def answer(tx, ctx, wait, answer):
    w = tx.need('waits', wait)
    if w['kind'] == 'delegate':
        raise Fault('Use delegation.complete with the current claim', 409)
    if w['state'] == 'answered':
        if w['answer'] != answer:
            raise Fault('Wait already answered differently', 409)
        return w
    if tx.need('runs', w['run'])['state'] != 'running':
        raise Fault('Run already terminal', 409)
    validate(schema(w['answer_schema']), answer)
    w.update(state='answered', answer=answer, actor=ctx.actor)
    return tx.put('waits', wait, w)


@op('delegation.feed', 'after?:int limit?:int', False)
def delegation_feed(tx, ctx, after=0, limit=16):
    """Ordered immutable request references. A zero limit reads the current watermark."""
    if after < 0 or limit < 0:
        raise Fault('Invalid delegation cursor')
    rows = tx.db.execute("SELECT value FROM records WHERE kind='delegation_events' AND id>? ORDER BY id LIMIT ?",
                         ('%020d' % after, min(64, limit)))
    return {'events': [json.loads(row[0]) for row in rows], 'high': tx.get('_meta', 'delegation_sequence', 0)}


def delegation(tx, key):
    w = tx.need('waits', key)
    if w['kind'] != 'delegate':
        raise Fault('Not a workflow delegation', 409)
    return w


@op('delegation.get', 'id:str', False)
def delegation_get(tx, ctx, id):
    """Read the authoritative request, context, expected output and current state."""
    w = delegation(tx, id)
    return dict(w, run_state=tx.need('runs', w['run'])['state'])


@op('delegation.claim', 'id:str claimant:str')
def delegation_claim(tx, ctx, id, claimant):
    """Acquire the right to work this request. Reuse the claimant after an interrupted call."""
    ident(claimant)
    w = delegation(tx, id)
    if w['state'] != 'open' or tx.need('runs', w['run'])['state'] != 'running':
        raise Fault('Delegation is no longer open', 409)
    if w.get('claimant') and w['claimant'] != claimant:
        raise Fault('Delegation already claimed', 409)
    w['claimant'] = claimant
    return tx.put('waits', id, w)


@op('delegation.release', 'id:str claimant:str')
def delegation_release(tx, ctx, id, claimant):
    """Release a claim after reconciling any external effects."""
    w = delegation(tx, id)
    if w['state'] != 'open' or w.get('claimant') != claimant:
        raise Fault('Not the active delegation claim', 409)
    del w['claimant']
    return tx.put('waits', id, w)


@op('delegation.complete', 'id:str claimant:str result?:obj error?:str')
def delegation_complete(tx, ctx, id, claimant, result=None, error=None):
    """Return the claimed result or failure; a matched retry has no further effect."""
    w = delegation(tx, id)
    if w.get('claimant') != claimant or (result is None) == (error is None):
        raise Fault('Supply the current claimant and exactly one result or error', 409)
    if w['state'] != 'open':
        if (result is not None and w.get('answer') == result) or (error is not None and w.get('error') == error):
            return w
        raise Fault('Delegation already resolved', 409)
    if tx.need('runs', w['run'])['state'] != 'running':
        raise Fault('Workflow no longer running', 409)
    if result is not None:
        validate(schema(w['answer_schema']), result)
        w.update(state='answered', answer=result)
    else:
        w.update(state='failed', error=text(error, 'error', 4000))
    return tx.put('waits', id, w)


@op('process.waits', 'run?:str', False)
def waits(tx, ctx, run=''):
    return [w for w in tx.rows('waits') if w['state'] == 'open' and
            (not run or w['run'] == run) and tx.need('runs', w['run'])['state'] == 'running']


@op('process.cancel', 'run:str')
def cancel(tx, ctx, run):
    r = tx.need('runs', run)
    if r['state'] == 'running':
        for child in r['children']:
            cancel(tx, ctx, child)
        for t in r['tokens']:
            if 'action' in t:
                a = tx.need('actions', t['action'])
                if a['state'] == 'queued':
                    a.update(state='failed', result={'cancelled': True})
                elif a['state'] == 'claimed':
                    if a['contract'] == 'human.decision:1' and a['provider'] == 'builtin':
                        a.update(state='failed', result={'cancelled': True})
                    else:
                        a.update(state='uncertain', result={'reason': 'cancelled after dispatch'})
                tx.put('actions', a['id'], a)
                if a.get('decision_id'):
                    from .decisions import close
                    row = tx.need('decisions', a['decision_id'])
                    if row['state'] == 'pending' and not any(
                            tx.need('actions', key)['state'] == 'claimed' for key in row['actions']):
                        close(tx, ctx, row, 'withdrawn')
                from .authorization import observe
                observe(tx, a['id'], a['state'], a.get('result'))
        finish(tx, r, 'cancelled', {})
        tx.put('runs', run, r)
    return r


@op('routine.learn', 'run:str name:str bindings?:obj')
def learn(tx, ctx, run, name, bindings=None):
    """Extract successful provider calls. Map activity/input names to new parameter names."""
    r = tx.need('runs', run)
    if r['state'] != 'completed' or not r['trace']:
        raise Fault('A completed run with observed provider calls is required')
    steps, inputs = [], {}
    for a in r['trace']:
        args = dict(a['inputs'])
        for field, param in (bindings or {}).get(a['activity'], {}).items():
            typ = tx.need('contracts', a['contract'])['inputs'][field]['type']
            inputs[ident(param)] = typ
            args[field] = {'$': 'input.' + param}
        steps.append({'type': 'service', 'contract': a['contract'], 'args': args, 'save': 'last'})
    return routine(tx, ctx, name, steps, inputs, {'last': 'obj'}, {'last': {'$': 'last'}})


@op('bpmn.import', 'name:str xml:str')
def import_bpmn(tx, ctx, name, xml):
    """Import one executable BPMN process. Biome extension attributes use urn:biomerte:v1."""
    from .xmlprofile import compile_xml
    return publish(tx, ctx, name, compile_xml(xml))
