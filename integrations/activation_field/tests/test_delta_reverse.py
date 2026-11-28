"""ΔW-only native quantized projection and actual tiny Qwen reverse tests."""
import numpy as np
import pytest
from types import SimpleNamespace
from activation_field.inverse import InverseOptions


def test_weight_options():
    assert InverseOptions.parse().weight_mode == 'current'
    assert InverseOptions.parse({'weight_mode':'delta'}).to_dict()['weight_mode'] == 'delta'
    for bad in ('reflected', 'baseline', '', None, True, [], {}):
        with pytest.raises(ValueError):InverseOptions.parse({'weight_mode':bad})


@pytest.mark.parametrize('dtype_name',['float32','bfloat16'])
@pytest.mark.parametrize('changed',[False,True])
def test_projection_signed_delta_vjp_bias_and_storage(dtype_name,changed):
    mx=pytest.importorskip('mlx.core');nn=pytest.importorskip('mlx.nn')
    from activation_field.adapters.delta_projection import install_delta_projection
    dtype=getattr(mx,dtype_name)
    m=nn.QuantizedLinear(64,64,bias=True,group_size=64,bits=4)
    # All codes start at 7; selected words have alternating +1/-1 changes.
    base=mx.full(m.weight.shape,0x77777777,dtype=mx.uint32)
    m.weight=mx.full(m.weight.shape,0x68686868,dtype=mx.uint32) if changed else base
    m.scales=mx.full(m.scales.shape,.03125,dtype=dtype)
    m.biases=mx.full(m.biases.shape,1.0,dtype=dtype)
    m.bias=mx.ones((64,),dtype=dtype)*3
    stored=(m.weight,m.scales,m.biases,m.bias)
    snapshots=[np.array(a.astype(mx.float32)) for a in stored]
    current=mx.dequantize(m.weight,m.scales.astype(mx.float32),m.biases.astype(mx.float32),group_size=64,bits=4)
    original=mx.dequantize(base,m.scales.astype(mx.float32),m.biases.astype(mx.float32),group_size=64,bits=4)
    delta=current-original
    x=mx.arange(64).reshape(1,64).astype(dtype)/64
    incoming=mx.ones((1,64),dtype=dtype)
    expected=(x.astype(mx.float32) @ delta.T).astype(dtype)
    expected_grad=(incoming.astype(mx.float32) @ delta).astype(dtype)
    install_delta_projection(m,base)
    output,grads=mx.vjp(m,[x],[incoming]);mx.eval(output,grads);output=output[0]
    np.testing.assert_allclose(np.array(output.astype(mx.float32)),np.array(expected.astype(mx.float32)),atol=1e-6)
    np.testing.assert_allclose(np.array(grads[0].astype(mx.float32)),np.array(expected_grad.astype(mx.float32)),atol=1e-6)
    assert output.dtype==dtype
    assert all(a is b for a,b in zip(stored,(m.weight,m.scales,m.biases,m.bias)))
    for old,a in zip(snapshots,stored):np.testing.assert_array_equal(old,np.array(a.astype(mx.float32)))
    if changed:
        assert bool(mx.any(delta>0)) and bool(mx.any(delta<0))
        assert bool(mx.any(output!=0))
    else:
        assert not bool(mx.any(output!=0))
        assert not bool(mx.any(grads[0]!=0))


@pytest.mark.parametrize('weight_mode',['current','delta'])
@pytest.mark.parametrize('changed',[False,True])
def test_actual_tiny_qwen_reverse(weight_mode,changed):
    mx=pytest.importorskip('mlx.core')
    from mlx_lm.models.cache import ArraysCache,RotatingKVCache
    from test_reverse_native import tiny
    from activation_field.adapters.qwen35_reverse import Qwen35ReverseAdapter
    from activation_field.inverse_runtime import reconstruct_with_adapter
    from activation_field.token_readout import PartitionTokenReadout
    model,field=tiny()
    caches=[ArraysCache(size=2) if layer.is_linear else RotatingKVCache(16) for layer in model.layers]
    for token in (1,2,3):
        mx.eval(model(mx.array([[token]]),cache=caches));field.observe('user')
    field.commit()
    from activation_field import boundaries
    from activation_field.adapters.qwen35 import boundary_dimensions
    records=boundaries.restore(boundaries.metadata(field.boundaries,field.version),
        dict(boundaries.tensors(field.boundaries)),field.version,boundary_dimensions(model))
    if changed:
        for _,m,*_ in field.entries:m.weight=mx.bitwise_xor(m.weight,mx.array(1,mx.uint32))
    stored=[(m,m.weight,m.scales,m.biases,type(m)) for _,m,*_ in field.entries]
    embedding=np.array(model.model.embed_tokens(mx.arange(64)).astype(mx.float32))
    readout=PartitionTokenReadout.from_embeddings(embedding)
    kwargs={} if weight_mode=='current' else {'weight_mode':'delta'}
    adapter=Qwen35ReverseAdapter(SimpleNamespace(model=model,field=field),records,readout,**kwargs)
    result=reconstruct_with_adapter(adapter,records,records,field.entries,str)
    assert result['weight_mode']==weight_mode
    assert result['projection_operator']==('Wcurrent-W0' if weight_mode=='delta' else 'Wcurrent')
    assert result['estimated_token_ids']
    for m,w,s,b,kind in stored:
        assert m.weight is w and m.scales is s and m.biases is b
        assert (type(m) is kind)==(weight_mode=='current')
