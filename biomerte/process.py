"""Versioned executable processes. Routines and BPMN imports target the same graph."""
import copy
import json
import operator
import time
from .api import op, ident, schema, validate, invoke
from .store import Fault, uid, digest, text
from . import providers
from .work import ready

TYPES = set('start end fail set choose fork join service call human message timer each select'.split())
INTERNAL = set(
    'knowledge.put knowledge.get knowledge.search bus.send bus.publish artifact.put artifact.get work.list'.split())


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
    elif kind in ('human', 'message'):
        if 'wait' not in t:
            t['wait'] = uid()
            tx.put('waits', t['wait'], {'id': t['wait'], 'run': r['id'], 'kind': kind,
                                        'name': n.get('name', t['at']), 'question': ev(n.get('question', '')),
                                        'answer_schema': n.get('outputs', {}), 'state': 'open'})
            tx.emit('wait.open', {'id': t['wait'], 'run': r['id'], 'kind': kind})
            return True
        wait = tx.need('waits', t['wait'])
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
            ctx.app.timer_anchors[t['delay_id']] = time.monotonic()
            return True
        # A replacement controller starts a conservative local delay if it never observed this wait.
        anchor = ctx.app.timer_anchors.setdefault(t['delay_id'], time.monotonic())
        if time.monotonic() - anchor < t['seconds']:
            return False
        ctx.app.timer_anchors.pop(t.pop('delay_id'), None)
        del t['seconds']
    elif kind == 'service':
        inputs = ev(n.get('args', {}))
        if 'operation' in n:
            result = invoke(tx, ctx, n['operation'], inputs)
        else:
            if 'action' not in t:
                try:
                    lease = providers.acquire(tx, ctx, r['task'] or r['id'], n['contract'],
                                              ev(n.get('resource', '')))
                except Fault as e:
                    if e.status in (404, 409):
                        from .extensions import prepare, demand
                        resource = ev(n.get('resource', ''))
                        status = prepare(tx, ctx, n['contract'], resource)
                        waiting = str(e)
                        if status['state'] == 'missing':
                            need = demand(tx, ctx, n['contract'], resource, r['id'], t['at'])
                            waiting = 'Waiting for capability work ' + need['id']
                        if t.get('blocked') == waiting:
                            return False
                        t['blocked'] = waiting
                        return True
                    raise
                a = providers.dispatch(tx, ctx, lease['id'], inputs, uid())
                t['action'] = a['id']
                t.pop('blocked', None)
                return True
            a = tx.need('actions', t['action'])
            if a['state'] in ('queued', 'claimed', 'uncertain'):
                return False
            if a['state'] != 'succeeded':
                raise Fault('Provider failed: ' + str(a.get('result')))
            result = a['result']
            r['trace'].append({'activity': t['at'], 'contract': a['contract'], 'inputs': inputs,
                               'operation': a['id'], 'resource': a['resource'], 'result': result})
            del t['action']
    if 'save' in n:
        variables[n['save']] = result
    t['at'] = dest
    return True


def tick(tx, ctx, budget=64):
    from .api import events_after
    budget = max(1, min(256, budget))
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
    runs = [r for r in tx.rows('runs') if r['state'] == 'running']
    cursor = tx.get('_meta', 'last_run', '')
    runs.sort(key=lambda r: (r['id'] <= cursor, r['id']))
    for r in runs:
        version = tx.need('process_versions', r['rev'])
        for t in list(r['tokens']):
            if r['state'] != 'running' or moves >= budget:
                break
            n = version['graph']['nodes'][t['at']]
            # Roll back partial effects and token mutations on a business error.
            backup = copy.deepcopy(r)
            change_backup = dict(tx.changes)
            tx.db.execute('SAVEPOINT activity')
            try:
                if r['steps'] >= 100000:
                    raise Fault('Step budget exhausted')
                changed = advance(tx, ctx, r, t, n, version)
            except (Fault, ValueError, KeyError, IndexError, TypeError) as exc:
                tx.db.execute('ROLLBACK TO activity')
                tx.changes = change_backup
                r = backup
                failed = next(v for v in r['tokens'] if v['id'] == t['id'])
                if 'on_error' in n:
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
    if w['state'] == 'answered':
        if w['answer'] != answer:
            raise Fault('Wait already answered differently', 409)
        return w
    if tx.need('runs', w['run'])['state'] != 'running':
        raise Fault('Run already terminal', 409)
    validate(schema(w['answer_schema']), answer)
    w.update(state='answered', answer=answer, actor=ctx.actor)
    return tx.put('waits', wait, w)


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
                    a.update(state='uncertain', result={'reason': 'cancelled after dispatch'})
                tx.put('actions', a['id'], a)
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
