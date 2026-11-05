"""Read-only tiled measurements; no full floating-point weight copy."""
import numpy as np
from .quantized_writer import unpack


def field_drift(field,tile_rows=16):
    import mlx.core as mx
    layers={}
    for path,module,_,_,layer,_ in field.entries:
        item=layers.setdefault(layer,dict(relative_rows=[],saturated_codes=0,codes=0))
        for start in range(0,module.weight.shape[0],tile_rows):
            sl=slice(start,start+tile_rows)
            q=unpack(np.array(module.weight[sl])).astype(np.float32)
            base=unpack(np.array(field.anchor[path][sl])).astype(np.float32)
            scales=np.repeat(np.array(module.scales[sl].astype(mx.float32)),64,axis=1)
            offsets=np.repeat(np.array(module.biases[sl].astype(mx.float32)),64,axis=1)
            anchor=base*scales+offsets
            drift=np.linalg.norm((q-base)*scales,axis=1)/np.maximum(np.linalg.norm(anchor,axis=1),1e-6)
            item['relative_rows'].extend(drift.tolist())
            item['saturated_codes']+=int(np.count_nonzero((q==0)|(q==15)))
            item['codes']+=q.size
    result=[]
    for layer,item in sorted(layers.items()):
        rows=item.pop('relative_rows')
        result.append(dict(layer=layer,rows=len(rows),relative_drift_percentiles=dict(zip(('p0','p50','p90','p95','p99','p100'),np.percentile(rows,[0,50,90,95,99,100]).tolist())),**item))
    return result
