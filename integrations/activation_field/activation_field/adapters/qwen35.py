import mlx.core as mx
import mlx.nn as nn


class Tap(nn.Module):
    def __init__(self, linear, path, collector):
        super().__init__()
        self.linear=linear
        object.__setattr__(self,"_path",path)
        object.__setattr__(self,"_collector",collector)

    def __call__(self,x):
        y=self.linear(x)
        self._collector(self._path,x,y)
        return y


def projections(model):
    layers=model.layers
    if len(layers)!=32: raise ValueError("Architecture mismatch: expected 32 layers")
    entries=[]
    for i,layer in enumerate(layers):
        linear=(i+1)%4!=0
        if layer.is_linear!=linear: raise ValueError("Hybrid layer order mismatch")
        family="linear_attn" if linear else "self_attn"
        parent=getattr(layer,family)
        names=["in_proj_qkv","in_proj_z","in_proj_a","in_proj_b","out_proj"] if linear else ["q_proj","k_proj","v_proj","o_proj"]
        for group,owner,names in [(family,parent,names),("mlp",layer.mlp,["gate_proj","up_proj","down_proj"])]:
            for name in names:
                path=f"language_model.model.layers.{i}.{group}.{name}"
                obj=getattr(owner,name)
                if not isinstance(obj,nn.QuantizedLinear) or obj.bits!=4 or obj.group_size!=64 or getattr(obj,"mode","affine")!="affine":
                    raise ValueError(f"Architecture/quantization mismatch: {path}")
                if obj.weight.dtype!=mx.uint32 or obj.weight.ndim!=2:
                    raise ValueError("Packed layout mismatch: "+path)
                entries.append((path,obj,owner,name,i,group))
    if len(entries)!=248: raise ValueError("Incomplete full-model coverage")
    return entries


def install(model,collector):
    entries=projections(model)
    for path,obj,parent,name,_,_ in entries:
        setattr(parent,name,Tap(obj,path,collector))
    return entries
