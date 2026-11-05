"""Read committed encrypted sparse checkpoints in tiles; never mutate Engine."""
import hashlib
import numpy as np
from .common import TransformError, canonical, digest, check_cancel
from .coordinates import unpack


def floating_metadata(array):
    # NumPy's buffer protocol cannot import MLX bfloat16. Conversion to fp32
    # preserves every bfloat16/fp16 value exactly before binary64 arithmetic.
    if 'bfloat16' in str(array.dtype):
        import mlx.core as mx
        array=array.astype(mx.float32)
    return np.asarray(array,dtype=np.float64)


class CheckpointReader:
    def __init__(self, vault, name):
        self.vault, self.name = vault, name
        self.manifest = vault.manifest(name)
        self.hash = digest(self.manifest)
        self.items = {t['name']: t for t in self.manifest['tensors']}
        if len(self.items) != len(self.manifest['tensors']):
            raise TransformError('invalid_artifact', 'Duplicate checkpoint tensors')

    def verify(self):
        if digest(self.vault.manifest(self.name)) != self.hash:
            raise TransformError('source_version_changed', 'Checkpoint manifest changed')

    def rows(self, key, start=0, end=None):
        item = self.items[key]
        shape = item['shape']
        end = shape[0] if end is None else end
        if not 0 <= start <= end <= shape[0]:
            raise TransformError('invalid_artifact', 'Checkpoint row range')
        result = np.empty([end-start]+shape[1:], np.dtype(item['dtype']))
        covered = 0
        for chunk in item['chunks']:
            left, right = max(start, chunk['start']), min(end, chunk['start']+chunk['rows'])
            if left >= right:
                continue
            aad = canonical([self.name, key, chunk['start'], shape, item['dtype']])
            raw = self.vault.open((self.vault.root/self.name/chunk['file']).read_bytes(), aad)
            if hashlib.sha256(raw).hexdigest() != chunk['sha256']:
                raise TransformError('invalid_artifact', 'Checkpoint chunk checksum mismatch')
            array = np.frombuffer(raw, np.dtype(item['dtype'])).reshape(chunk['rows'], *shape[1:])
            result[left-start:right-start] = array[left-chunk['start']:right-chunk['start']]
            covered += right-left
        if covered != end-start:
            raise TransformError('incomplete_materialization', 'Incomplete checkpoint rows')
        return result


class RuntimeSource:
    def __init__(self, engine, vault, target=None, tile_bytes=None, check=None):
        self.engine, self.vault, self.target = engine, vault, target
        self.check = check
        self.reader = CheckpointReader(vault, target) if target else None
        m = engine.manifest
        self.baseline_ref = dict(baseline_hash=m['baseline_hash'], repository=m['repository'], revision=m['revision'],
                                 architecture='qwen3.5-9b', tensor_map_version=1,
                                 quantization=dict(mode='affine', bits=4, group_size=64, packing='uint32-low-nibble-first'))
        self.entries = {p: module for p, module, *_ in engine.field.entries}
        self.descriptors = []
        self.has_negative_scales = False
        # Workspace policy is derived from current available memory, never a model/region limit.
        if tile_bytes is None:
            try:
                import psutil
                available = psutil.virtual_memory().available
            except ImportError:
                import os
                try:
                    available = os.sysconf('SC_AVPHYS_PAGES')*os.sysconf('SC_PAGE_SIZE')
                except (ValueError, OSError):
                    import subprocess,re
                    output=subprocess.check_output(['/usr/bin/vm_stat'],text=True)
                    page=int(re.search(r'page size of (\d+)',output).group(1))
                    counts=dict((name,int(n)) for name,n in re.findall(r'^([^:]+):\s+(\d+)',output,re.M))
                    available=page*sum(counts.get(k,0) for k in ('Pages free','Pages inactive','Pages speculative'))
            tile_bytes = max(4096, int(available*.01))
        self.tile_bytes = tile_bytes
        self.stats = {}
        if self.reader:
            meta = self.reader.manifest['metadata']
            if meta['baseline_hash'] != m['baseline_hash'] or meta.get('tensor_map_version') != 1:
                raise TransformError('baseline_mismatch', 'Target uses a different baseline')
            if meta.get('kind') not in ('field_only', 'complete'):
                raise TransformError('unsupported_layout', 'Expected committed field checkpoint')
            allowed = {p+suffix for p in self.entries for suffix in ('.indices', '.codes', '.frequency')}
            for key in self.reader.items:
                if key not in allowed and not key.startswith('cache.') and key != 'last_logits':
                    raise TransformError('unsupported_layout', 'Target contains unsupported or frozen tensor changes')
        for i, (path, module) in enumerate(sorted(self.entries.items())):
            shape = [int(module.weight.shape[0]), int(module.weight.shape[1])*8]
            if shape[1] % 64:
                raise TransformError('unsupported_layout', 'Expected complete affine4/group64 rows')
            # Scales/offsets are immutable in this checkpoint format, bound by baseline hash.
            self.has_negative_scales |= bool(np.any(floating_metadata(module.scales)<0))
            quant = dict(bits=4, group_size=64, packing='uint32-low-nibble-first',
                         metadata_encoding='binary64-exact-v1',
                         scales_dtype=str(module.scales.dtype),offsets_dtype=str(module.biases.dtype),
                         scales_sha256=hashlib.sha256(floating_metadata(module.scales).astype('<f8').tobytes()).hexdigest(),
                         offsets_sha256=hashlib.sha256(floating_metadata(module.biases).astype('<f8').tobytes()).hexdigest())
            self.descriptors.append(dict(id=i, path=path, logical_shape=shape, orientation='row-column',
                                         dtype='uint32', frozen=False, quantization_ref=quant, s_m=1.))
        self.identity = dict(baseline_ref=self.baseline_ref, target_hash=self.reader.hash if self.reader else None,
                             field_version=self.reader.manifest['metadata']['field_version'] if self.reader else 0)

    def verify(self):
        if self.reader:
            self.reader.verify()
        check_cancel(self.check)

    def tiles(self, descriptor):
        self.verify()
        path = descriptor['path']; module = self.entries[path]
        rows, cols = descriptor['logical_shape']; groups_per_row = cols//64
        tile_rows = max(1, min(rows, self.tile_bytes//(cols*160)))
        indices = np.empty(0, np.uint32)
        if self.reader and path+'.indices' in self.reader.items:
            indices = self.reader.rows(path+'.indices')
            codes = self.reader.items.get(path+'.codes')
            if indices.dtype != np.uint32 or indices.ndim != 1 or np.any(indices[1:] <= indices[:-1]) or (len(indices) and indices[-1] >= rows*groups_per_row):
                raise TransformError('invalid_artifact', 'Invalid or unordered target group indices')
            if not codes or codes['dtype'] != 'uint32' or codes['shape'] != [len(indices), 8]:
                raise TransformError('incomplete_materialization', 'Invalid target codes')
        elif self.reader and path+'.codes' in self.reader.items:
            raise TransformError('incomplete_materialization', 'Codes missing group indices')
        for start in range(0, rows, tile_rows):
            check_cancel(self.check)
            end = min(rows, start+tile_rows)
            words = np.array(self.engine.field.anchor[path][start:end], dtype=np.uint32)
            base_codes = unpack(words)
            changed = words.copy().reshape(-1, 8)
            lo, hi = np.searchsorted(indices, [start*groups_per_row, end*groups_per_row])
            if hi > lo:
                changed[indices[lo:hi]-start*groups_per_row] = self.reader.rows(path+'.codes', int(lo), int(hi))
            target_codes = unpack(changed.reshape(words.shape))
            scale = np.repeat(floating_metadata(module.scales[start:end]), 64, axis=1)
            offset = np.repeat(floating_metadata(module.biases[start:end]), 64, axis=1)
            if not np.isfinite(scale).all() or not np.isfinite(offset).all():
                raise TransformError('unsupported_layout', 'Invalid affine quantization metadata')
            if np.any((scale == 0) & (target_codes != base_codes)):
                raise TransformError('unsupported_layout', 'Target changes a zero-scale group')
            base = offset+scale*base_codes
            yield dict(start=start, base=base, base_codes=base_codes, target=offset+scale*target_codes,
                       target_codes=target_codes, scale=scale, offset=offset)
        self.verify()

    def characterize(self, progress=None):
        for d in self.descriptors:
            energy, count, changed = 0., 0, 0
            signed=dict(positive_count=0,negative_count=0,positive_squared=0.,negative_squared=0.,min_delta=0.,max_delta=0.)
            for tile in self.tiles(d):
                for value in np.sum(tile['base']**2,axis=1):energy += float(value)
                count += tile['base'].size
                changed += int(np.count_nonzero(tile['target_codes'] != tile['base_codes']))
                delta=tile['target']-tile['base']
                for label,mask in [('positive',delta>0),('negative',delta<0)]:
                    signed[label+'_count']+=int(np.count_nonzero(mask));signed[label+'_squared']+=float(np.sum(delta[mask]**2))
                signed['min_delta']=min(signed['min_delta'],float(delta.min()));signed['max_delta']=max(signed['max_delta'],float(delta.max()))
                if progress:
                    progress(dict(stage='characterize', tensor=d['path'], row=tile['start']))
            d['s_m'] = max(float(np.sqrt(energy/count)), 1e-12)
            self.stats[d['id']] = dict(count=count, changed=changed, unchanged=count-changed,**signed)
        return self.stats

    def manifest(self, config):
        from .format import EVALUATOR, COORDINATES, VALUE, COMBINATION, RESTORATION, SIGNED_RESTORATION
        return dict(schema_version=1, evaluator_id=EVALUATOR, baseline_ref=self.baseline_ref,
                    tensor_map=self.descriptors, coordinate_rule=COORDINATES, value_rule=VALUE,
                    combination_rule=COMBINATION, restoration_rule=SIGNED_RESTORATION if getattr(self,'has_negative_scales',False) else RESTORATION,
                    coverage=dict(selected=[d['path'] for d in self.descriptors], omitted=[],
                                  frozen='All noneligible baseline tensors are immutable and not represented by field checkpoints',
                                  source_properties='fixed original scales and offsets; committed sparse packed groups'),
                    provenance=dict(target_checkpoint_hash=self.identity['target_hash'], source_field_version=self.identity['field_version'],
                                    fitter_version='afwt-streaming/1', configuration_digest=digest(config)),
                    size_goal=dict(target_bytes=config['target_bytes'], enforcement='none'))
