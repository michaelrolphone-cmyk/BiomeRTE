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



def boundary_dimensions(model):
    result={}
    for i,layer in enumerate(model.layers):
        dim=layer.input_layernorm.weight.size
        if layer.is_linear:
            a=layer.linear_attn
            contexts=dict(conv_mean=(1,1,a.conv_dim),
                          recurrent=(1,a.num_v_heads,a.head_v_dim,a.head_k_dim))
        else:
            a=layer.self_attn
            contexts=dict(key_mean=(1,a.num_key_value_heads,1,a.head_dim),
                          value_mean=(1,a.num_key_value_heads,1,a.head_dim))
        result['layer.'+str(i)]=(dim,dim,contexts)
    dim=model.model.norm.weight.size
    result['final_norm']=(dim,dim,{})
    return result


def install_boundaries(model,collector):
    """Observe existing module calls without changing their parameter paths."""
    from mlx_lm.models.qwen3_5 import DecoderLayer

    class ObservedLayer(DecoderLayer):
        def __call__(self,x,mask=None,cache=None):
            y=super().__call__(x,mask=mask,cache=cache)
            if self._boundary_capture():
                if self.is_linear:
                    a=self.linear_attn
                    context=dict(conv_mean=mx.mean(cache[0],axis=1,keepdims=True),recurrent=cache[1])
                else:
                    keys,values=cache.keys_and_values()
                    context=dict(key_mean=mx.mean(keys,axis=2,keepdims=True),
                                 value_mean=mx.mean(values,axis=2,keepdims=True))
                self._boundary_collector(self._boundary_name,x,y,context)
            return y

    class ObservedNorm(nn.RMSNorm):
        def __call__(self,x):
            y=super().__call__(x)
            if self._boundary_capture():self._boundary_collector('final_norm',x,y,{})
            return y

    capture=lambda:(collector.__self__.capture and collector.__self__.config.enabled and not collector.__self__.config.read_only)
    for i,layer in enumerate(model.layers):
        if type(layer) is not DecoderLayer:raise ValueError('Unsupported Qwen layer boundary implementation')
        object.__setattr__(layer,'__class__',ObservedLayer)
        object.__setattr__(layer,'_boundary_collector',collector)
        object.__setattr__(layer,'_boundary_capture',capture)
        object.__setattr__(layer,'_boundary_name','layer.'+str(i))
    norm=model.model.norm
    if type(norm) is not nn.RMSNorm:raise ValueError('Unsupported final normalization')
    object.__setattr__(norm,'__class__',ObservedNorm)
    object.__setattr__(norm,'_boundary_collector',collector)
    object.__setattr__(norm,'_boundary_capture',capture)
