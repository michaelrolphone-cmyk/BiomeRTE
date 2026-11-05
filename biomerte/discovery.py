"""Paged live discovery and execution status, separate from store readiness."""
import base64
import json
from .api import op, OPERATIONS
from .store import Fault, digest, encode, uid
from . import providers

SECTIONS = ('operations', 'contracts', 'offers', 'guide')
PAGE_BYTES = 16000  # Transport page target, never an inventory or item limit.

@op('discovery.page', 'section?:str cursor?:str', False)
def page(tx, ctx, section='', cursor=''):
    from .mcp import CALLER_GUIDE
    after = ''
    if cursor:
        try:
            value = json.loads(base64.urlsafe_b64decode(cursor.encode('ascii')).decode('utf8'))
            if section and section != value['section']:
                raise ValueError('section mismatch')
            section, after = value['section'], value['after']
            if not isinstance(after, str):
                raise ValueError('invalid key')
        except Exception:
            raise Fault('Invalid discovery cursor')
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
    items, size = [], 0
    for row in remaining:
        amount = len(encode(row).encode('utf8'))
        if items and size + amount > PAGE_BYTES:
            break
        items.append(row)
        size += amount
    following = None
    if len(items) < len(remaining):
        following = {'section': section, 'after': items[-1][key]}
    elif section != SECTIONS[-1]:
        following = {'section': SECTIONS[SECTIONS.index(section)+1], 'after': ''}
    cursor = base64.urlsafe_b64encode(encode(following).encode('utf8')).decode('ascii') if following else None
    result = {'section': section, 'items': items, 'section_total': len(rows),
              'next_cursor': cursor, 'complete': following is None,
              'consistency': 'live keyset; inventory may change between pages'}
    if cursor:
        result['next_call'] = {'operation': 'discovery.page', 'arguments': {'cursor': cursor},
                               'request_id': 'discovery-' + uid()}
    return result

@op('system.progress', write=False)
def progress(tx, ctx):
    """Report outstanding effects and assistant business outcomes, not just liveness."""
    from .assistant_support import assistant_status
    actions = tx.rows('actions')
    groups = {}
    for action in actions:
        if action['state'] not in ('queued', 'claimed', 'uncertain'):
            continue
        key = (action['provider'], action['contract'])
        row = groups.setdefault(key, {'provider': key[0], 'contract': key[1], 'states': {}})
        row['states'][action['state']] = row['states'].get(action['state'], 0) + 1
    assistants = []
    for row in tx.rows('assistant_responsibilities'):
        state = assistant_status(tx, ctx, row['id'])
        assistants.append({k: state[k] for k in ('id', 'desired', 'state', 'active_cycle', 'blocked_dispatches')})
    blocked = any(a['state'] == 'uncertain' for a in actions) or any(a['blocked_dispatches'] for a in assistants)
    return {'state': 'degraded' if blocked else 'pending' if groups else 'ready',
            'outstanding': sorted(groups.values(), key=lambda x: (x['provider'], x['contract'])),
            'assistants': assistants,
            'meaning': 'Store readiness is separate. Pending/claimed commands are not proof of progress or success.'}
