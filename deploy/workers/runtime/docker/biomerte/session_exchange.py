"""Finite signed outcome exchange for an explicitly authorized local session.

No completed-session history is created here. Session state is kept locally only
while coordination/import is unresolved; callers remove it after distribution.
"""
import time
import json
from .identity import signed, verify
from .store import Fault, digest, encode

# Runtime churn and security/ownership records must never be shared through a
# durable outcome session. New durable namespaces need explicit classification.
DURABLE_KINDS = frozenset(('work', 'revisions', 'documents', 'definitions',
                           'process_versions', 'grants'))


def identity(kind, key):
    return encode([kind, key])


def value_digest(value):
    return digest(value)


def validate_scope(scope):
    if not isinstance(scope, list) or not 1 <= len(scope) <= 256:
        raise Fault('A coordination session requires explicit durable scope')
    result = set()
    for pair in scope:
        if (not isinstance(pair, list) or len(pair) != 2 or
                pair[0] not in DURABLE_KINDS or
                not isinstance(pair[1], str) or not pair[1] or len(pair[1]) > 512):
            raise Fault('Session scope contains non-durable or invalid records')
        key = identity(*pair)
        if key in result:
            raise Fault('Duplicate session scope')
        result.add(key)
    return result


def validate_session(session, node):
    if (not isinstance(session, dict) or session.get('state') not in ('open', 'closing') or
            node not in session.get('participants', []) or
            not isinstance(session.get('id'), str) or not session['id']):
        raise Fault('No active local coordination session', 409)
    # Expiry prevents new edits; final export/import is a recovery operation and
    # remains allowed while its local state is explicitly closing.
    if session['state'] == 'open' and session.get('expires', 0) <= time.time():
        raise Fault('Session expired; close before exporting', 409)
    return validate_scope(session.get('scope'))


def outcome_identity(data):
    return digest({'session':data['session'],'authorization_digest':data.get('authorization_digest'),
        'final_seq':data['final_seq'],'final_hash':data['final_hash'],
        'records':sorted([row[:3] for row in data['records']])})


def export_package(tx, app, session, recipient, final_head):
    scope = validate_session(session, app.node)
    if recipient not in session['participants']:
        raise Fault('Unknown session recipient', 403)
    if session.get('final_head') != final_head or session['state'] != 'closing':
        raise Fault('Session must be frozen at its final head', 409)
    baseline = session.get('baselines', {}).get(recipient)
    if not isinstance(baseline, dict) or set(baseline) != scope:
        raise Fault('Missing recipient baseline', 409)
    validate_records(tx, [], session['scope'], app, session.get('peer_keys'))
    records = []
    for kind, key in sorted(session['scope']):
        row = tx.db.execute('SELECT value FROM records WHERE kind=? AND id=?', (kind, key)).fetchone()
        raw = row[0] if row else None
        records.append([kind, key, raw, baseline[identity(kind, key)]])
    payload = {'format': 1, 'site': app.site, 'sender': app.node, 'to': recipient,
               'kind': 'session-state', 'data': {'session': session['id'],
               'final_seq': final_head['seq'], 'final_hash': final_head['hash'],
               'closer': session.get('closer', session.get('owner', session['participants'][0])),
               'closer_epoch':session.get('closer_epoch',1),
               'authorization_digest': session.get('authorization_digest'),
               'scope': session['scope'], 'records': records}}
    payload['data']['outcome_digest'] = outcome_identity(payload['data'])
    return signed(app.key, payload)


def import_package(tx, app, session, packet):
    import json
    scope = validate_session(session, app.node)
    payload = packet.get('payload', {})
    data = payload.get('data', {})
    if (payload.get('format') != 1 or payload.get('kind') != 'session-state' or
            payload.get('site') != app.site or payload.get('to') != app.node or
            payload.get('sender') != session.get('closer', session.get('owner', session['participants'][0])) or
            data.get('session') != session['id']):
        raise Fault('Package does not belong to this session', 403)
    if (data.get('outcome_digest') != outcome_identity(data) or
            data.get('closer_epoch') != session.get('closer_epoch',1) or
            data.get('closer') != payload['sender'] or
            data.get('authorization_digest') != session.get('authorization_digest')):
        raise Fault('Package closer authorization differs from invitation', 403)
    peer = ({'key': app.config['public_key'], 'enabled': True}
            if payload['sender'] == app.node else tx.need('peers', payload['sender']))
    if (not peer.get('enabled') or
            (payload['sender'] != app.node and 'coordination' not in peer.get('scopes', []))):
        raise Fault('Session peer is revoked', 403)
    # Membership is a finite local authorization, not an expansion of a peer's
    # permanent sync permissions. Its expected key is fixed at session open.
    if session.get('peer_keys', {}).get(payload['sender']) != peer['key']:
        raise Fault('Session peer key changed', 403)
    verify(peer['key'], packet.get('signature'), payload)
    final_head = {'seq': data.get('final_seq'), 'hash': data.get('final_hash')}
    if session.get('final_head') != final_head or session['state'] != 'closing':
        raise Fault('Package final head is not the frozen session head', 409)
    if validate_scope(data.get('scope')) != scope:
        raise Fault('Package scope differs from session', 403)
    records = data.get('records')
    if not isinstance(records, list) or len(records) != len(scope):
        raise Fault('Incomplete session package')
    parsed, seen = [], set()
    local_baseline = session.get('baselines', {}).get(app.node, {})
    for row in records:
        if not isinstance(row, list) or len(row) != 4:
            raise Fault('Invalid session record')
        kind, key, raw, expected = row
        item = identity(kind, key)
        if item not in scope or item in seen or expected != local_baseline.get(item):
            raise Fault('Session record identity or baseline mismatch', 409)
        seen.add(item)
        if raw is not None and not isinstance(raw, str):
            raise Fault('Session values must be opaque JSON text')
        value = json.loads(raw) if raw is not None else None
        if raw is not None and encode(value) != raw:
            raise Fault('Session value is not canonical JSON')
        current = tx.get(kind, key)
        # Identical already-imported values are naturally idempotent. No
        # perpetual package-ID receipt is needed after distribution completes.
        if value_digest(current) not in (expected, value_digest(value)):
            raise Fault('Local durable state changed during coordination', 409)
        parsed.append((kind, key, value))
    validate_records(tx, parsed, session['scope'], app, session.get('peer_keys'))
    for kind, key, value in parsed:
        if value is None:
            tx.drop(kind, key)
        else:
            tx.put(kind, key, value)
            if kind == 'revisions' and value['writer'].split(':')[0] == app.node:
                # Local writer cursors are not journaled, but must advance when
                # our signed prepared revisions are adopted into this database.
                writer = value['writer']
                tx.put('_writers', writer, max(tx.get('_writers', writer, 0), value['seq']))
    return {'session': session['id'], 'final_head': final_head,
            'package_digest': digest(payload), 'applied': len(parsed),
            'outcome_digest':data['outcome_digest'], 'closer_epoch':data['closer_epoch']}


def _rows_bounded(tx,kind):
    cursor = tx.db.execute('SELECT value FROM records WHERE kind=? ORDER BY id',(kind,))
    while True:
        batch = cursor.fetchmany(128)
        if not batch:
            return
        for row in batch:
            yield json.loads(row[0])


def validate_records(tx, changes, scope, app=None, peer_keys=None):
    """Validate the entire scoped final view, not just individual JSON writes.

    References must be explicitly scoped so every recipient receives the same
    closure. Runtime traces/attempt IDs remain opaque historical evidence.
    """
    allowed = validate_scope(scope)
    changed = {(kind, key): value for kind, key, value in changes}
    view = {(kind, key): changed.get((kind, key), tx.get(kind, key)) for kind, key in scope}

    def need(kind, key):
        if identity(kind, key) not in allowed or view.get((kind, key)) is None:
            raise Fault('Session durable reference is outside its resulting scope: %s %s' % (kind, key), 409)
        return view[(kind, key)]

    # Documents are projections of their complete immutable revision DAG.
    # Silently omitting an existing local branch would erase a conflict.
    documents = {key for kind, key in scope if kind == 'documents'}
    for row in _rows_bounded(tx,'revisions'):
        if row.get('key') in documents and ('revisions', row.get('rev')) not in view:
            raise Fault('Session omits an existing document revision', 409)
    # A scoped parent cannot be accepted while an unscoped local child remains
    # unfinished, nor deleted while that child still refers to it.
    for child in _rows_bounded(tx,'work'):
        parent = child.get('parent')
        if parent and ('work', parent) in view and ('work', child.get('id')) not in view:
            parent_value = view[('work', parent)]
            if parent_value is None or (parent_value.get('state') == 'accepted' and child.get('state') != 'accepted'):
                raise Fault('Session parent outcome omits an unfinished child', 409)

    for (kind, key), value in view.items():
        old = tx.get(kind, key)
        if kind in ('revisions', 'process_versions') and old is not None and value != old:
            raise Fault('Immutable durable record cannot change or be deleted', 409)
        if value is None:
            if old is not None:
                raise Fault('Durable domain records cannot be deleted by state exchange', 409)
            continue
        if not isinstance(value, dict):
            raise Fault('Durable record must be an object')
        if kind == 'revisions':
            body = dict(value)
            rid = body.pop('rev', None)
            body.pop('signature', None)
            if (type(value.get('seq')) is not int or value['seq'] < 1 or
                    not isinstance(value.get('writer'), str) or not isinstance(value.get('parents'), list)):
                raise Fault('Invalid revision writer or parents')
            if rid != key or digest(body) != key:
                raise Fault('Revision hash mismatch')
            document = need('documents', value['key'])
            for parent in value['parents']:
                if need('revisions', parent)['key'] != value['key']:
                    raise Fault('Revision parent document mismatch')
            if app is not None:
                author = value['writer'].split(':')[0]
                public = (peer_keys or {}).get(author)
                if not public:
                    raise Fault('Revision writer is outside session authority', 403)
                verify(public, value.get('signature'), {'revision': key, 'writer': value['writer']})
        elif kind == 'documents':
            heads = value.get('heads')
            if value.get('id') != key or not isinstance(heads, list) or not heads or len(set(heads)) != len(heads):
                raise Fault('Invalid document heads')
            for head in heads:
                if need('revisions', head)['key'] != key:
                    raise Fault('Document head belongs to another document')
            members = {k for (t, k), v in view.items() if t == 'revisions' and v and v.get('key') == key}
            parents = {p for k in members for p in view[('revisions', k)]['parents']}
            if set(heads) != members - parents or value.get('conflict') != (len(heads) > 1):
                raise Fault('Document head projection does not match revision DAG')
        elif kind == 'process_versions':
            from .process import validate_graph
            validate_graph(value['graph'])
            if value.get('id') != key or digest([value['name'], value['graph'], value['calls']]) != key:
                raise Fault('Process version hash mismatch')
            called = {n['process'] for n in value['graph']['nodes'].values() if n['type'] == 'call'}
            if called != set(value['calls']):
                raise Fault('Process call bindings mismatch')
            for name, rev in value['calls'].items():
                if need('process_versions', rev)['name'] != name:
                    raise Fault('Process child binding mismatch')
        elif kind == 'definitions':
            version = need('process_versions', value['rev'])
            if (value.get('id') != key or version['name'] != key or
                    value.get('inputs') != version['graph'].get('inputs', {}) or
                    value.get('outputs') != version['graph'].get('outputs', {})):
                raise Fault('Definition binding mismatch')
        elif kind == 'work':
            from .work import PARENTS
            required = {'id', 'kind', 'title', 'parent', 'priority', 'sequence', 'dependencies',
                        'criteria', 'context', 'state', 'revision', 'assigned', 'attempts', 'evidence'}
            if (not required.issubset(value) or value.get('kind') not in PARENTS or
                    value.get('state') != 'accepted' or
                    type(value.get('revision')) is not int or value['revision'] < 1 or
                    not isinstance(value.get('title'), str) or not value['title'].strip() or
                    any(not isinstance(value.get(k), list) for k in ('dependencies', 'criteria', 'context', 'attempts')) or
                    not isinstance(value.get('evidence'), dict)):
                raise Fault('Invalid durable work record')
            if value.get('id') != key:
                raise Fault('Work identity mismatch')
            if old and value != old:
                if any(value.get(k) != old.get(k) for k in ('id', 'kind', 'parent')):
                    raise Fault('Work hierarchy identity is immutable')
                if value['revision'] <= old['revision']:
                    raise Fault('Work revision must advance', 409)
                if old['state'] == 'accepted' and value['state'] != 'accepted':
                    raise Fault('Accepted work cannot be reopened by state import', 409)
            if value.get('parent'):
                from .work import PARENTS
                if need('work', value['parent']).get('kind') != PARENTS.get(value.get('kind')):
                    raise Fault('Work parent kind mismatch')
            for dep in value.get('dependencies', []):
                dependency = need('work', dep)
                if value.get('state') == 'accepted' and dependency.get('state') != 'accepted':
                    raise Fault('Accepted work has unmet dependencies')
            if value.get('state') == 'accepted':
                if (not value['evidence'] or set(value['criteria']) - set(value['evidence']) or
                        any(not isinstance(v, str) or not v.strip() for v in value['evidence'].values())):
                    raise Fault('Accepted work lacks criterion evidence')
                children = [v for (k, unused), v in view.items() if k == 'work' and v and v.get('parent') == key]
                if any(child['state'] != 'accepted' for child in children):
                    raise Fault('Accepted work has unaccepted children')
        elif kind == 'grants':
            need('work', value['task'])
            need('process_versions', value['rev'])
            if value.get('id') != key or value.get('state') != 'returned':
                raise Fault('Invalid durable grant')
            if old and any(value.get(k) != old.get(k) for k in ('id', 'to', 'task', 'rev')):
                raise Fault('Grant binding is immutable')
            if old and old.get('state') == 'returned' and value != old:
                raise Fault('Returned grant outcome is immutable')
            if value['state'] == 'returned':
                outcome = value.get('outcome', {})
                if outcome.get('trace'):
                    raise Fault('Process activity traces stay on the local offline exchange path')
                if outcome.get('grant') != key or outcome.get('rev') != value['rev']:
                    raise Fault('Grant outcome binding mismatch')

    def visit(kind, key, active, done):
        if key in active:
            raise Fault('Cyclic durable references')
        if key in done:
            return
        value = need(kind, key)
        refs = (value.get('dependencies', []) + ([value['parent']] if value.get('parent') else [])
                if kind == 'work' else list(value['calls'].values()))
        for ref in refs:
            visit(kind, ref, active | {key}, done)
        done.add(key)
    for kind in ('work', 'process_versions'):
        done = set()
        for (item_kind, key), value in view.items():
            if item_kind == kind and value is not None:
                visit(kind, key, set(), done)
