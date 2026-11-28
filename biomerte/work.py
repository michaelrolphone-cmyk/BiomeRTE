"""Work hierarchy, dependency readiness, acceptance and mission allocation."""
from .api import op, ident
from .store import Fault, uid, text

PARENTS = {'goal': None, 'milestone': 'goal', 'workflow': 'milestone', 'task': 'workflow'}


def ready(tx, item):
    return item['state'] == 'open' and all(tx.need('work', dep)['state'] == 'accepted'
                                           for dep in item['dependencies'])


def check_plan(tx, item):
    """Validate hierarchy and the proposed prerequisite graph before saving."""
    items = {row['id']: row for row in tx.rows('work')}
    key, parent = item['id'], item['parent']
    if parent and (parent not in items or items[parent]['kind'] != PARENTS[item['kind']]):
        raise Fault('Invalid hierarchy')
    deps = item['dependencies'] + item.get('sequence_dependencies', [])
    if any(not isinstance(dep, str) for dep in deps):
        raise Fault('Invalid dependency identifier')
    if len(set(deps)) != len(deps):
        raise Fault('Each prerequisite may appear once')
    for dep in deps:
        if ident(dep) != key and dep not in items:
            raise Fault('Unknown prerequisite: ' + dep)
    items[key] = item
    children = {}
    for row in items.values():
        children.setdefault(row['parent'], []).append(row['id'])

    # Parent acceptance waits for children. Dependency paths must also account
    # for those implicit edges, including the new edge introduced by a move.
    pending = deps + children.get(key, [])
    visited = set()
    while pending:
        dep = pending.pop()
        if dep == key:
            raise Fault('Dependency cycle')
        if dep not in visited:
            visited.add(dep)
            row = items[dep]
            pending.extend(row['dependencies'])
            pending.extend(row.get('sequence_dependencies', []))
            pending.extend(children.get(dep, []))
    if parent in visited:
        raise Fault('Dependency cycle through parent')
    if item['state'] != 'accepted' and parent and items[parent]['state'] == 'accepted':
        raise Fault('Reopen the parent before adding unfinished work', 409)
    if item['state'] == 'accepted' and any(items[dep]['state'] != 'accepted'
                                           for dep in item['dependencies']):
        raise Fault('Accepted work has unmet prerequisites', 409)


@op('work.create', 'kind:str title:str id?:str parent?:str priority?:int sequence?:int dependencies?:list criteria?:list context?:list sequence_dependencies?:list')
def create(tx, ctx, kind, title, id='', parent='', priority=0, sequence=0, dependencies=None,
           criteria=None, context=None, sequence_dependencies=None):
    """Create a goal, milestone, planning workflow or task. Larger priority is selected first."""
    key = ident(id) if id else uid()
    if tx.get('work', key):
        raise Fault('Work ID exists', 409)
    if kind not in PARENTS:
        raise Fault('Invalid work kind')
    for criterion in criteria or []:
        text(criterion, 'criterion', 2000)
    item = {'id': key, 'kind': kind, 'title': text(title, 'title', 500), 'parent': parent,
            'priority': priority, 'sequence': sequence, 'dependencies': dependencies or [],
            'criteria': criteria or [], 'context': context or [], 'state': 'open', 'revision': 1,
            'assigned': '', 'attempts': [], 'evidence': {}}
    if sequence_dependencies:
        item['sequence_dependencies'] = sequence_dependencies
    check_plan(tx, item)
    tx.put('work', key, item)
    from .extensions import record_plan
    record_plan(tx, ctx, item)
    tx.emit('work.created', {'id': key})
    return item


@op('work.update', 'id:str revision:int changes:obj')
def update(tx, ctx, id, revision, changes):
    item = tx.need('work', id)
    if item['revision'] != revision:
        raise Fault('Stale work revision', 409)
    allowed = {'title': str, 'priority': int, 'sequence': int, 'dependencies': list,
               'context': list, 'assigned': str, 'parent': str, 'sequence_dependencies': list}
    for k, v in changes.items():
        if k not in allowed or not isinstance(v, allowed[k]):
            raise Fault('Invalid work edit: ' + k)
    if 'title' in changes:
        text(changes['title'], 'title', 500)
    item.update(changes)
    if changes.keys() & {'dependencies', 'sequence_dependencies', 'parent'}:
        check_plan(tx, item)
    item['revision'] += 1
    tx.put('work', id, item)
    from .extensions import record_plan, PLAN_FIELDS
    if changes.keys() & (set(PLAN_FIELDS) | {'parent', 'sequence_dependencies'}):
        record_plan(tx, ctx, item)
    tx.emit('work.updated', {'id': id, 'revision': item['revision']})
    return item


@op('work.list', 'kind?:str', False)
def listing(tx, ctx, kind=''):
    items = tx.rows('work')
    accepted = {item['id'] for item in items if item['state'] == 'accepted'}
    return [
        dict(item, ready=item['state'] == 'open' and not blocked,
             blocked_by=blocked, sequence_pending=sequence_pending)
        for item in items if not kind or item['kind'] == kind
        for blocked in [[dep for dep in item['dependencies'] if dep not in accepted]]
        for sequence_pending in [[dep for dep in item.get('sequence_dependencies', [])
                                  if dep not in accepted]]
    ]


@op('work.accept', 'id:str revision:int evidence:obj')
def accept(tx, ctx, id, revision, evidence):
    """Accept an outcome separately from process completion. Map every criterion to evidence."""
    item = tx.need('work', id)
    if revision != item['revision']:
        raise Fault('Stale work revision', 409)
    if item['state'] in ('running', 'delegated'):
        raise Fault('Attempt or delegated work outstanding', 409)
    if not evidence or any(not isinstance(v, str) or not v.strip() for v in evidence.values()):
        raise Fault('Acceptance needs evidence')
    if set(item['criteria']) - set(evidence):
        raise Fault('Missing criterion evidence')
    if any(tx.need('work', dep)['state'] != 'accepted' for dep in item['dependencies']):
        raise Fault('Unmet prerequisite', 409)
    if any(x['parent'] == id and x['state'] != 'accepted' for x in tx.rows('work')):
        raise Fault('Unaccepted child work', 409)
    item.update(state='accepted', evidence=evidence, revision=revision + 1)
    tx.put('work', id, item)
    from .providers import release_task
    release_task(tx, id)
    tx.emit('work.accepted', {'id': id, 'evidence': evidence})
    return item


@op('mission.apply', 'budget:obj services:list restore_margin?:obj stable_samples?:int')
def mission(tx, ctx, budget, services, restore_margin=None, stable_samples=1):
    """Allocate protected minima first, then upgrades. Dependencies are counted once."""
    def numbers(m):
        return isinstance(m, dict) and all(isinstance(v, (int, float)) and v >= 0 for v in m.values())
    if not numbers(budget):
        raise Fault('Invalid capacity')
    catalog = {}
    for s in services:
        key = ident(s['id'])
        modes = s['modes']
        if key in catalog or not modes or not isinstance(s['priority'], int):
            raise Fault('Duplicate service or invalid modes/priority')
        for i, mode in enumerate(modes):
            text(mode['name'])
            if not numbers(mode['cost']) or any(v > mode['cost'].get(k, 0) for k, v in
                                                (modes[i - 1]['cost'].items() if i else [])):
                raise Fault('Modes must have nondecreasing costs')
        catalog[key] = s
    order = sorted(catalog, key=lambda i: (-catalog[i]['priority'], i))
    if not numbers(restore_margin or {}) or not 1 <= stable_samples <= 1000:
        raise Fault('Invalid recovery policy')
    previous = tx.get('mission', 'current', {})
    previous_modes = previous.get('allocation', {}) if previous.get('services') == services else {}
    streaks = dict(previous.get('recovery_samples', {})) if previous_modes else {}
    remaining = dict(budget)
    active = {}

    def closure(key, visiting, found):
        if key in visiting or key not in catalog:
            raise Fault('Unknown or cyclic service dependency')
        if key not in found:
            for dep in catalog[key].get('needs', []):
                closure(dep, visiting | {key}, found)
            found.add(key)

    def fits(cost):
        return all(v <= remaining.get(k, 0) for k, v in cost.items())

    def spend(cost):
        for k, v in cost.items():
            remaining[k] = remaining.get(k, 0) - v
    # At each priority tier, fund minima (and their dependencies) before upgrades.
    # Deferrable work at lower priority competes only for remaining capacity.
    for priority in sorted(set(catalog[i]['priority'] for i in order), reverse=True):
        tier = [i for i in order if catalog[i]['priority'] == priority]
        for key in tier:
            needed = set()
            closure(key, set(), needed)
            cost = {}
            for dep in needed - set(active):
                for k, v in catalog[dep]['modes'][0]['cost'].items():
                    cost[k] = cost.get(k, 0) + v
            if fits(cost):
                spend(cost)
                active.update({d: 0 for d in needed if d not in active})
        for key in tier:
            if key not in active:
                continue
            modes = catalog[key]['modes']
            for i in range(active[key] + 1, len(modes)):
                delta = {k: v - modes[i - 1]['cost'].get(k, 0) for k, v in modes[i]['cost'].items()}
                if not fits(delta):
                    break
                spend(delta)
                active[key] = i
    # Decreases take effect immediately. Withhold an upgrade until there is sustained headroom.
    for key in order:
        modes = catalog[key]['modes']
        previous_index = next((i for i, m in enumerate(
            modes) if m['name'] == previous_modes.get(key)), -1)
        index = active.get(key, -1)
        if key not in previous_modes or index <= previous_index:
            streaks[key] = 0
            continue
        margin_ok = all(remaining.get(k, 0) >= amount for k,
                        amount in (restore_margin or {}).items())
        streaks[key] = (streaks.get(key, 0) + 1) if margin_ok else 0
        if streaks[key] < stable_samples:
            if previous_index < 0:
                active.pop(key, None)
            else:
                active[key] = previous_index
    # A recovering dependency may still be held below admission.
    for unused in range(len(active)):
        removed = [k for k in active if any(d not in active for d in catalog[k].get('needs', []))]
        if not removed:
            break
        for k in removed:
            del active[k]
    remaining = dict(budget)
    for k, index in active.items():
        spend(catalog[k]['modes'][index]['cost'])
    result = {'budget': budget, 'remaining': remaining, 'services': services, 'recovery_samples': streaks,
              'allocation': {k: catalog[k]['modes'][active[k]]['name'] if k in active else None for k in order},
              'unmet': [k for k in order if k not in active]}
    tx.put('mission', 'current', result)
    tx.emit('mission.changed', {'allocation': result['allocation'], 'unmet': result['unmet']})
    return result
