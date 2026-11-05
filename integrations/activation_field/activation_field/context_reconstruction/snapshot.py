"""Read only packed changes; never decrypt source caches, logits or transcripts."""
import hashlib
import numpy as np
from ..checkpoint import canonical

def weight_arrays(vault, name):
    manifest=vault.manifest(name)
    for item in manifest['tensors']:
        if not item['name'].endswith(('.indices','.codes')):continue
        a=np.empty(item['shape'],np.dtype(item['dtype']))
        for c in item['chunks']:
            aad=canonical([name,item['name'],c['start'],item['shape'],item['dtype']])
            data=vault.open((vault.root/name/c['file']).read_bytes(),aad)
            if hashlib.sha256(data).hexdigest()!=c['sha256']:raise ValueError('Corrupt weight checkpoint')
            a[c['start']:c['start']+c['rows']]=np.frombuffer(data,a.dtype).reshape(c['rows'],*item['shape'][1:])
        yield item['name'],a

def snapshot(source,name,destination):
    manifest=source.manifest(name);meta=manifest['metadata']
    allowed=('baseline_hash','tensor_map_version','kind','config')
    selected={k:meta[k] for k in allowed}
    selected['kind']='field_only'
    destination.save('source',selected,weight_arrays(source,name))
    return dict(checkpoint_id=name,sha256=hashlib.sha256(canonical(manifest)).hexdigest(),
                baseline_hash=meta['baseline_hash'])
