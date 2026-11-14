"""Paged live discovery and execution status, separate from store readiness."""
import base64
import json
from .api import op, OPERATIONS
from .store import Fault, digest, encode, uid
from . import providers

SECTIONS = ('operations', 'contracts', 'offers', 'guide')
DEFAULT_DISCOVERY_LIMIT = 25

@op('system.discovery', 'section?:str cursor?:str limit?:int', False)
@op('discovery.page', 'section?:str cursor?:str limit?:int', False)
def page(tx, ctx, section='', cursor='', limit=None):
    from .mcp import CALLER_GUIDE
    after = ''
    if cursor:
        try:
            value = json.loads(base64.urlsafe_b64decode(cursor.encode('ascii')).decode('utf8'))
            if section and section != value['section']:
                raise ValueError('section mismatch')
            section, after = value['section'], value['after']
            if limit is None:
                limit = value.get('limit', DEFAULT_DISCOVERY_LIMIT)
            if not isinstance(after, str):
                raise ValueError('invalid key')
        except Exception:
            raise Fault('Invalid discovery cursor')
    if limit is None:
        limit = DEFAULT_DISCOVERY_LIMIT
    if type(limit) is not int or limit < 1:
        raise Fault('Discovery limit must be a positive integer')
    section = section or 'operations'
    if section not in SECTIONS:
        raise Fault('Unknown discovery section')
    if section == 'operations':
        # Use the same public schema as bus.discover.
        from .api import discover
        rows = discover(tx, ctx)
        key = 'name'
    elif section in ('contracts', 'offers'):
        rows = providers.discover(tx, ctx)[section]
        key = 'id'
    else:
        rows, key = [dict(id='caller-guide', **CALLER_GUIDE)], 'id'
    rows = sorted(rows, key=lambda row: row[key])
    remaining = [row for row in rows if row[key] > after]
    items = remaining[:limit]
    following = None
    if len(items) < len(remaining):
        following = {'section': section, 'after': items[-1][key]}
    elif section != SECTIONS[-1]:
        following = {'section': SECTIONS[SECTIONS.index(section)+1], 'after': ''}
    if following:
        following['limit'] = limit
    cursor = base64.urlsafe_b64encode(encode(following).encode('utf8')).decode('ascii') if following else None
    result = {'section': section, 'items': items, 'section_total': len(rows),
              'next_cursor': cursor, 'complete': following is None,
              'consistency': 'live keyset; inventory may change between pages'}
    if cursor:
        result['next_call'] = {'operation': 'system.discovery', 'arguments': {'cursor': cursor},
                               'request_id': 'discovery-' + uid()}
    return result

@op('system.progress', write=False)
def progress(tx, ctx):
    """Report current effects using indexed actions and compact run states."""
    import json
    actions = tx.matching('actions', 'state', ('queued', 'claimed', 'uncertain'))
    groups = {}
    for action in actions:
        key = (action['provider'], action['contract'])
        row = groups.setdefault(key, {'provider': key[0], 'contract': key[1], 'states': {}})
        row['states'][action['state']] = row['states'].get(action['state'], 0) + 1
    # Run-state projection contains scalar metadata, never historical results.
    # Dirty rows retain the same correctness fallback as Records.matching().
    run_states = {key: json.loads(value) for key, value in tx.db.execute(
        "SELECT id,value FROM record_lookup WHERE kind='runs' AND field='state'")}
    for key, raw in tx.db.execute(
            "SELECT r.id,r.value FROM record_lookup_dirty d CROSS JOIN records r "
            "ON r.kind=d.kind AND r.id=d.id WHERE d.kind='runs'"):
        run_states[key] = json.loads(raw)['state']
    responsibilities = tx.rows('assistant_responsibilities')
    backgrounds = tx.rows('assistant_background') if responsibilities else []
    episodes = tx.rows('assistant_episodes') if responsibilities else []
    failures = tx.rows('assistant_dispatch_failures') if responsibilities else []
    assistants = []
    for row in responsibilities:
        entries = [item for item in backgrounds + episodes if item['responsibility_id'] == row['id']]
        active = False
        for item in entries:
            run_id = item['run_id']
            if run_id not in run_states:
                run_states[run_id] = tx.need('runs', run_id)['state']
            active = active or run_states[run_id] == 'running'
        blocked_dispatches = [item for item in failures if item['responsibility_id'] == row['id']]
        state = ('running' if active or row.get('active_cycle') else
                 'blocked' if blocked_dispatches else row.get('coverage', {}).get('kind', 'idle'))
        assistants.append({'id': row['id'], 'desired': row['desired'], 'state': state,
                           'active_cycle': row.get('active_cycle'), 'blocked_dispatches': blocked_dispatches})
    blocked = any(a['state'] == 'uncertain' for a in actions) or any(a['blocked_dispatches'] for a in assistants)
    return {'state': 'degraded' if blocked else 'pending' if groups else 'ready',
            'outstanding': sorted(groups.values(), key=lambda x: (x['provider'], x['contract'])),
            'assistants': assistants,
            'meaning': 'Store readiness is separate. Pending/claimed commands are not proof of progress or success.'}
