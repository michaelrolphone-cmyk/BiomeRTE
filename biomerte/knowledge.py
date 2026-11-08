"""Content-addressed revision DAGs. An edit can name one or multiple parents."""
import hashlib
from .api import op, ident
from .store import Fault, digest, text, b64, un64


def revision(tx, record):
    body = dict(record)
    rid = body.pop('rev', None)
    body.pop('signature', None)
    if digest(body) != rid:
        raise Fault('Revision hash mismatch')
    if tx.get('revisions', rid):
        return rid
    key = text(body['key'])
    for parent in body['parents']:
        if tx.need('revisions', parent)['key'] != key:
            raise Fault('Parent belongs to another document')
    heads = set(tx.get('documents', key, {'heads': []})['heads'])
    heads.difference_update(body['parents'])
    heads.add(rid)
    tx.put('revisions', rid, record)
    tx.put('documents', key, {'id': key, 'heads': sorted(heads), 'conflict': len(heads) > 1})
    return rid


@op('knowledge.put', 'key:str body:any parents?:list links?:list type?:str')
def put(tx, ctx, key, body, parents=None, links=None, type='document'):
    """Commit an immutable revision. Supply all conflicting heads to resolve a conflict."""
    wr = ':'.join([ctx.app.node, ctx.scope, ctx.app.config['incarnation']])
    counter = tx.get('_writers', wr, 0) + 1
    tx.put('_writers', wr, counter)
    rec = {'key': ident(key), 'body': body, 'type': type, 'links': links or [],
           'parents': sorted(set(parents or [])), 'writer': wr, 'seq': counter, 'author': ctx.actor}
    rec['rev'] = digest(rec)
    from .identity import sign
    rec['signature'] = sign(ctx.app.key, {'revision': rec['rev'], 'writer': wr})
    revision(tx, rec)
    tx.emit('knowledge.revised', {'key': key, 'rev': rec['rev']})
    return rec


@op('knowledge.get', 'key:str rev?:str', False)
def get(tx, ctx, key, rev=''):
    if rev:
        rec = tx.need('revisions', rev)
        if rec['key'] != key:
            raise Fault('Document mismatch')
        return rec
    doc = tx.need('documents', key)
    return dict(doc, revisions=[tx.need('revisions', r) for r in doc['heads']])


@op('knowledge.search', 'query?:str', False)
def search(tx, ctx, query=''):
    q = query.lower()
    return [d for d in tx.rows('documents') if q in d['id'].lower() or
            any(q in str(tx.need('revisions', h)['body']).lower() for h in d['heads'])]


@op('artifact.put', 'data:str transient?:bool')
def artifact(tx, ctx, data, transient=False):
    """Put an immutable base64 chunk (up to 256 KiB); larger artifacts use a manifest of chunks."""
    raw = un64(data)
    if len(raw) > 262144:
        raise Fault('Split artifact into 256 KiB chunks', 413)
    key = hashlib.sha256(raw).hexdigest()
    tx.put('artifacts', key, {'id': key, 'data': b64(raw)})
    marker = tx.get('_retention', 'artifacts:' + key)
    if marker is not None:
        # A durable upload always wins, including shared content-addressed bytes.
        if not transient:
            # Removing enrollment is permanent: later overwrites cannot enroll
            # an existing artifact, exactly like pre-upgrade durable content.
            tx.drop('_retention', marker['id'])
        elif not marker.get('durable'):
            marker['operational'] = True
            tx.put('_retention', marker['id'], marker)
    return {'sha256': key, 'size': len(raw)}


@op('artifact.get', 'sha256:str', False)
def artifact_get(tx, ctx, sha256):
    value = tx.get('artifacts', sha256)
    if value is None and tx.get('_meta', 'operational_retention'):
        raise Fault('Artifact unavailable or expired; recover from its owning durable source', 410)
    return tx.need('artifacts', sha256)


@op('knowledge.merge', 'key:str heads:list resolutions?:obj')
def merge(tx, ctx, key, heads, resolutions=None):
    """Three-way merge sibling object edits. Resolve a conflicting field by choosing a head revision."""
    doc = tx.need('documents', key)
    if len(heads) < 2 or set(doc['heads']) != set(heads):
        raise Fault('Concurrent heads changed', 409)
    records = [tx.need('revisions', h) for h in heads]
    parents = records[0]['parents']
    if len(parents) != 1 or any(r['parents'] != parents for r in records):
        raise Fault('Use knowledge.put with all heads for a multi-generation merge', 409)
    base = tx.need('revisions', parents[0])['body']
    if not isinstance(base, dict) or any(not isinstance(r['body'], dict) for r in records):
        raise Fault('Use an explicit merged body for non-object contents', 409)
    missing = object()
    body = {}
    conflicts = []
    fields = set(base)
    for r in records:
        fields.update(r['body'])
    for field in fields:
        before = base.get(field, missing)
        changed = []
        for r in records:
            value = r['body'].get(field, missing)
            if value != before and value not in changed:
                changed.append(value)
        if len(changed) > 1:
            choice = (resolutions or {}).get(field)
            if choice not in heads:
                conflicts.append(field)
                continue
            value = tx.need('revisions', choice)['body'].get(field, missing)
        else:
            value = changed[0] if changed else before
        if value is not missing:
            body[field] = value
    if conflicts:
        return {'state': 'conflict', 'fields': sorted(conflicts), 'heads': heads}
    return put(tx, ctx, key, body, heads, records[0]['links'], records[0]['type'])
