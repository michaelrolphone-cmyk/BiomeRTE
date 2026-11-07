"""Read packed changes and accumulated directions, never caches/logits/transcripts."""
import hashlib
import numpy as np
from ..checkpoint import canonical

def field_arrays(vault, name, include_directions=True):
    manifest=vault.manifest(name)
    for item in manifest['tensors']:
        if not (item['name'].endswith(('.indices','.codes')) or
                (include_directions and item['name'].startswith(('direction.','boundary.')))):continue
        a=np.empty(item['shape'],np.dtype(item['dtype']))
        for c in item['chunks']:
            aad=canonical([name,item['name'],c['start'],item['shape'],item['dtype']])
            data=vault.open((vault.root/name/c['file']).read_bytes(),aad)
            if hashlib.sha256(data).hexdigest()!=c['sha256']:raise ValueError('Corrupt weight checkpoint')
            a[c['start']:c['start']+c['rows']]=np.frombuffer(data,a.dtype).reshape(c['rows'],*item['shape'][1:])
        yield item['name'],a

def weight_arrays(vault, name):
    return field_arrays(vault, name, include_directions=False)

def snapshot(source,name,destination):
    manifest=source.manifest(name);meta=manifest['metadata']
    allowed=('baseline_hash','tensor_map_version','kind','config')
    selected={k:meta[k] for k in allowed}
    for key in ('directions','boundaries'):
        if key in meta:selected[key]=meta[key]
    selected['kind']='field_only'
    selected['snapshot_schema_version']=2
    selected['snapshot_state']='final_accumulated_aggregates'
    destination.save('source',selected,field_arrays(source,name))
    return dict(checkpoint_id=name,sha256=hashlib.sha256(canonical(manifest)).hexdigest(),
                baseline_hash=meta['baseline_hash'])

