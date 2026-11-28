"""Deterministic, inspectable two-member AFWT container; no residual payload."""
import hashlib
import io
import json
import struct
import zipfile
from pathlib import Path
import numpy as np
from .common import TransformError, canonical, atomic_bytes
from .regions import Region

RECORD = struct.Struct('<II9f4B')
EVALUATOR = 'afwt.elliptic-value/1'
COORDINATES = 'tensor-cell-center-v1'
VALUE = 'baseline-rms-v1'
COMBINATION = 'additive-region-id-order-v1'
RESTORATION = 'affine4-fixed-base-binary64-ties-even-v1'
SIGNED_RESTORATION = 'affine4-native-signed-scale-binary64-ties-even-v1'


def validate_manifest(m):
    if m.get('schema_version') != 1 or m.get('evaluator_id') != EVALUATOR:
        raise TransformError('invalid_artifact', 'Unsupported evaluator')
    for key, expected in [('coordinate_rule', COORDINATES), ('value_rule', VALUE),
                          ('combination_rule', COMBINATION)]:
        if m.get(key) != expected:
            raise TransformError('invalid_artifact', 'Unsupported '+key)
    if m.get('restoration_rule') not in (RESTORATION,SIGNED_RESTORATION):
        raise TransformError('invalid_artifact','Unsupported restoration rule')
    if not isinstance(m.get('baseline_ref'), dict) or not m['baseline_ref'].get('baseline_hash'):
        raise TransformError('invalid_artifact', 'Missing baseline identity')
    tensors = m.get('tensor_map')
    if not isinstance(tensors, list):
        raise TransformError('invalid_artifact', 'Missing tensor map')
    ids, paths = set(), set()
    for d in tensors:
        if type(d.get('id')) is not int or d['id'] in ids or not 0 <= d['id'] < 2**32 or d.get('path') in paths:
            raise TransformError('invalid_artifact', 'Duplicate/invalid tensor identity')
        shape = d.get('logical_shape')
        if not isinstance(shape, list) or len(shape) != 2 or any(type(n) is not int or n <= 0 for n in shape):
            raise TransformError('invalid_artifact', 'Invalid logical shape')
        if d.get('orientation') != 'row-column' or d.get('dtype') != 'uint32' or type(d.get('frozen')) is not bool:
            raise TransformError('unsupported_layout', 'Unsupported tensor layout')
        if not np.isfinite(d.get('s_m', float('nan'))) or d['s_m'] < 1e-12:
            raise TransformError('invalid_artifact', 'Invalid baseline normalization')
        if not isinstance(d.get('path'), str) or not d['path'] or not isinstance(d.get('quantization_ref'), dict):
            raise TransformError('invalid_artifact', 'Missing tensor mapping')
        ids.add(d['id']); paths.add(d['path'])
    if m.get('size_goal', {}).get('enforcement') != 'none':
        raise TransformError('invalid_artifact', 'Size goal must be advisory')
    for name in ('coverage', 'provenance'):
        if not isinstance(m.get(name), dict):
            raise TransformError('invalid_artifact', 'Missing '+name)


def encode(manifest, regions):
    records = []
    for region in sorted(regions, key=lambda r: r.id):
        region.validate()
        try:
            records.append(RECORD.pack(*region.to_dict().values()))
        except (OverflowError, struct.error) as exc:
            raise TransformError('invalid_artifact', 'Parameters not representable in float32') from exc
    binary = b''.join(records)
    m = dict(manifest, region_encoding=dict(byte_order='little', format='float32-v1', record_bytes=48,
             count=len(regions), sha256=hashlib.sha256(binary).hexdigest()))
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as z:
        for name, data in [('manifest.json', canonical(m)), ('regions.bin', binary)]:
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o600 << 16
            z.writestr(info, data)
    data = stream.getvalue()
    decode(data)  # Validate the actual encoded, float32-rounded representation.
    return data


def decode(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if z.namelist() != ['manifest.json', 'regions.bin'] or any(i.compress_type != zipfile.ZIP_STORED for i in z.infolist()):
                raise TransformError('invalid_artifact', 'AFWT requires exactly two stored members')
            m = json.loads(z.read('manifest.json'), parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
            binary = z.read('regions.bin')
        validate_manifest(m)
        e = m['region_encoding']
        if e.get('byte_order') != 'little' or e.get('format') != 'float32-v1' or e.get('record_bytes') != 48 or type(e.get('count')) is not int or e['count'] < 0:
            raise TransformError('invalid_artifact', 'Unsupported region encoding')
        if len(binary) != 48*e['count'] or hashlib.sha256(binary).hexdigest() != e['sha256']:
            raise TransformError('invalid_artifact', 'Region checksum/length mismatch')
        regions = [Region(*v).validate() for v in RECORD.iter_unpack(binary)]
        tensors = {d['id']: d for d in m['tensor_map']}
        previous = -1
        for r in regions:
            if r.id <= previous or r.tensor_id not in tensors or tensors[r.tensor_id]['frozen']:
                raise TransformError('invalid_artifact', 'Region order or tensor ownership invalid')
            previous = r.id
        return m, regions
    except TransformError:
        raise
    except (KeyError, TypeError, ValueError, zipfile.BadZipFile, OverflowError, struct.error) as exc:
        raise TransformError('invalid_artifact', str(exc)) from exc


def read_artifact(path):
    path = Path(path)
    data = path.read_bytes()
    if path.name.endswith('.afwt.enc'):
        from ..checkpoint import Vault
        data = Vault(path.parent/'private').open(data, path.name.encode())
    return data


def load_transform(path):
    return decode(read_artifact(path))


def save_transform(manifest, regions, path):
    data = encode(manifest, regions)
    path = Path(path)
    if path.name.endswith('.afwt.enc'):
        from ..checkpoint import Vault
        data = Vault(path.parent/'private').seal(data, path.name.encode())
    atomic_bytes(path, data)
    return inspect_transform(path)


def inspect_transform(path):
    data = read_artifact(path)
    m, regions = decode(data)
    manifest_bytes = len(canonical(m))
    return dict(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data), MB=len(data)/1e6,
                MiB=len(data)/2**20, parameter_bytes=len(regions)*48, manifest_bytes=manifest_bytes,
                container_bytes=len(data)-manifest_bytes-len(regions)*48, region_count=len(regions),
                baseline_ref=m['baseline_ref'], coverage=m['coverage'], provenance=m['provenance'],
                size_goal=m['size_goal'], above_goal=len(data)>m['size_goal']['target_bytes'])
