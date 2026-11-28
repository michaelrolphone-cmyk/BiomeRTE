"""Transfer large capability results through Biome's immutable artifact store.

32 KiB frames keep base64 RPC envelopes below the gateway's observed request
ceiling. This is transport framing, never an artifact or fitting size limit.
"""
import base64
import hashlib
import json
from .common import canonical, TransformError

FRAME_BYTES = 32 * 1024
FANOUT = 128
SCHEMA = 'afwt.transfer/1'


def publish_bytes(data, put, media_type):
    def save(value):
        expected = hashlib.sha256(value).hexdigest()
        receipt = put(value)
        if receipt.get('sha256') != expected or receipt.get('size') != len(value):
            raise TransformError('invalid_artifact', 'Transport storage receipt mismatch')
        return dict(sha256=expected, size=len(value))
    parts = [save(data[i:i+FRAME_BYTES]) for i in range(0, len(data), FRAME_BYTES)]
    kind = 'chunks'
    while len(parts) > FANOUT:
        parts = [save(canonical(dict(schema=SCHEMA, kind=kind, parts=parts[i:i+FANOUT])))
                 for i in range(0, len(parts), FANOUT)]
        kind = 'nodes'
    root = save(canonical(dict(schema=SCHEMA, kind=kind, parts=parts)))
    return dict(schema=SCHEMA, root=root, sha256=hashlib.sha256(data).hexdigest(),
                bytes=len(data), media_type=media_type, read_operation='artifact.get')


def read_transfer(reference, get):
    """Reassemble and verify a transfer; get(sha256) returns raw artifact bytes."""
    if reference.get('schema') != SCHEMA:
        raise TransformError('invalid_artifact', 'Unsupported transfer schema')
    def fetch(ref):
        data = get(ref['sha256'])
        if len(data) != ref['size'] or hashlib.sha256(data).hexdigest() != ref['sha256']:
            raise TransformError('invalid_artifact', 'Corrupt transfer part')
        return data
    def visit(ref, ancestors):
        if ref['sha256'] in ancestors:
            raise TransformError('invalid_artifact', 'Cyclic transfer manifest')
        node = json.loads(fetch(ref))
        if node.get('schema') != SCHEMA or node.get('kind') not in ('chunks', 'nodes'):
            raise TransformError('invalid_artifact', 'Unsupported transfer node')
        for part in node['parts']:
            if node['kind'] == 'chunks':yield fetch(part)
            else:yield from visit(part, ancestors | {ref['sha256']})
    data = b''.join(visit(reference['root'], set()))
    if len(data) != reference['bytes'] or hashlib.sha256(data).hexdigest() != reference['sha256']:
        raise TransformError('invalid_artifact', 'Transfer content mismatch')
    return data


def transport_result(value, put=None):
    export = value.get('encoding') == 'base64' and value.get('filename', '').endswith('.afwt')
    serialized = canonical(value)
    if not export and len(serialized) <= FRAME_BYTES:return value
    if put is None:
        from ..provider import gateway
        g = gateway()
        def put(data):
            sha = hashlib.sha256(data).hexdigest()
            return g.invoke('artifact.put', dict(data=base64.b64encode(data).decode()),
                            rid='afwt-artifact-'+sha, realm='site')
    if export:
        data = base64.b64decode(value['data'], validate=True)
        if len(data) != value['bytes'] or hashlib.sha256(data).hexdigest() != value['sha256']:
            raise TransformError('invalid_artifact', 'Export content mismatch')
        result = {k:v for k,v in value.items() if k != 'data'}
        result.update(encoding='biome-artifact-tree/1', artifact=publish_bytes(data, put, 'application/x-afwt'))
        if len(canonical(result)) <= FRAME_BYTES:return result
        # An unusually large coverage manifest is itself transferred in full.
        value = result;serialized = canonical(result)
    summary = {k:value[k] for k in ('ok','run_id','status','version','action','error') if k in value}
    return dict(**summary, result_encoding='biome-artifact-json/1',
                result_artifact=publish_bytes(serialized, put, 'application/json'))
