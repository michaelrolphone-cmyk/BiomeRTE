"""Dependency-scoped impact inventory; never grants permission or infers safety."""
import copy
from .store import Fault, digest


def snapshot(tx, cfg, frame):
    configured = {x['id']: copy.deepcopy(x) for x in cfg['systems']}
    offered = {x['resource'] for x in tx.rows('offers')}
    proposal = frame.get('proposal') or {}
    descriptor = proposal.get('action') or {}
    discovered = frame.get('discovered_systems') or []
    discovered_ids = []
    for item in discovered:
        identifier = item if isinstance(item, str) else item.get('id')
        if not isinstance(identifier, str) or not identifier:
            raise Fault('Discovered system requires an identity')
        discovered_ids.append(identifier)
        # Model discoveries expand the review but cannot certify relationships.
        if identifier not in configured:
            configured[identifier] = {'id': identifier, 'dependencies': [],
                                      'source': 'discovery', 'unknown_relationships': True}
    if descriptor:
        roots = list(proposal.get('affected_systems', [])) + [descriptor['resource']]
        roots += [x for x in proposal.get('targets', []) if x in configured or x in offered]
        roots += discovered_ids
    else:
        # Explicit inventory/discovery without a concrete action remains global.
        roots = list(configured) + list(offered)
    if not all(isinstance(x, str) and x for x in roots):
        raise Fault('Impact scope requires system identities')
    pending = sorted(set(roots))
    selected = {}
    while pending:
        identifier = pending.pop()
        if identifier in selected:
            continue
        row = copy.deepcopy(configured.get(identifier) or {
            'id': identifier, 'dependencies': [],
            'source': 'registry' if identifier in offered else 'dependency',
            'unknown_relationships': True})
        dependencies = row.get('dependencies', [])
        if not isinstance(dependencies, list) or not all(isinstance(x, str) and x for x in dependencies):
            raise Fault('Invalid dependencies for system ' + identifier)
        row['dependencies'] = dependencies
        selected[identifier] = row
        pending.extend(x for x in dependencies if x not in selected)
    inventory = sorted(selected.values(), key=lambda x: x['id'])
    frame['inventory'] = inventory
    frame['inventory_revision'] = digest(inventory)
    frame['impact_roots'] = sorted(set(roots))
    frame['unknown_dependencies'] = [x['id'] for x in inventory if x.get('unknown_relationships')]
    return inventory


def begin(tx, cfg, frame):
    inventory = snapshot(tx, cfg, frame)
    frame['impact_reviewed'] = {}
    frame['impact_queue'] = copy.deepcopy(inventory)
    frame['impact_index'] = 0
    frame['system_findings'] = []


def reviewed_systems(frame):
    if 'impact_reviewed' not in frame:
        # Recover completed entries from an in-flight pre-upgrade queue.
        done = frame.get('impact_queue', [])[:frame.get('impact_index', 0)]
        frame['impact_reviewed'] = {x['id']: digest(x) for x in done}
    return frame['impact_reviewed']


def refresh(tx, cfg, frame):
    reviewed = reviewed_systems(frame)
    current = frame.get('current_system')
    if current:
        reviewed[current['id']] = digest(current)
    frame.setdefault('system_findings', []).append(copy.deepcopy(frame.get('findings', [])))
    inventory = snapshot(tx, cfg, frame)
    # Revisit changed descriptions and newly discovered dependencies. A review
    # of the previous graph revision does not cover a newly changed system.
    frame['impact_queue'] = [copy.deepcopy(x) for x in inventory
                             if reviewed.get(x['id']) != digest(x)]
    frame['impact_index'] = 0


def covered(tx, cfg, frame):
    inventory = snapshot(tx, cfg, frame)
    reviewed = reviewed_systems(frame)
    pending = [x['id'] for x in inventory if reviewed.get(x['id']) != digest(x)]
    frame['unreviewed_systems'] = pending
    frame['impact_complete'] = not pending and not frame['unknown_dependencies']
    return frame['impact_complete']
