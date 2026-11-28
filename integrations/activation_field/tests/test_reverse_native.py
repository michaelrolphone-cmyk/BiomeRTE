"""Mechanical tests using tiny instances of the actual installed Qwen classes.

These do not load a pretrained model or measure semantic reconstruction.
"""
import numpy as np
import pytest
mx=pytest.importorskip('mlx.core')
nn=pytest.importorskip('mlx.nn')
from types import SimpleNamespace
from mlx_lm.models.qwen3_5 import TextModelArgs,TextModel
from mlx_lm.models.cache import ArraysCache,RotatingKVCache
from activation_field.adapters.qwen35 import Tap,install_boundaries,boundary_dimensions
from activation_field.adapters.qwen35_reverse import Qwen35ReverseAdapter
from activation_field.field import Field
from activation_field.config import FieldConfig
from activation_field import boundaries
from activation_field.inverse_runtime import reconstruct_with_adapter
from activation_field.token_readout import PartitionTokenReadout


def tiny(num_hidden_layers=4):
    args=TextModelArgs(hidden_size=64,intermediate_size=128,num_hidden_layers=num_hidden_layers,
        num_attention_heads=2,num_key_value_heads=1,head_dim=32,vocab_size=64,
        linear_num_value_heads=2,linear_num_key_heads=1,linear_key_head_dim=32,
        linear_value_head_dim=32,linear_conv_kernel_dim=4)
    model=TextModel(args);nn.quantize(model,group_size=64,bits=4)
    entries=[]
    for i,layer in enumerate(model.layers):
        family='linear_attn' if layer.is_linear else 'self_attn'
        a=getattr(layer,family)
        names=('in_proj_qkv','in_proj_z','in_proj_a','in_proj_b','out_proj') if layer.is_linear else ('q_proj','k_proj','v_proj','o_proj')
        for group,owner,ns in ((family,a,names),('mlp',layer.mlp,('gate_proj','up_proj','down_proj'))):
            for name in ns:
                path=f'language_model.model.layers.{i}.{group}.{name}'
                entries.append((path,getattr(owner,name),owner,name,i,group))
    field=Field(entries,FieldConfig(enabled=True,eta=0,commit_every_tokens=2))
    for path,module,parent,name,*_ in entries:setattr(parent,name,Tap(module,path,field.tap))
    install_boundaries(model,field.boundary_tap)
    return model,field


def test_native_boundary_capture_checkpoint_and_reverse_wiring():
    model,field=tiny()
    cache=[ArraysCache(size=2) if layer.is_linear else RotatingKVCache(16) for layer in model.layers]
    for token in (1,2,3):
        output=model(mx.array([[token]]),cache=cache);mx.eval(output)
        field.observe('user')
    field.commit()
    assert len(field.boundaries)==5
    assert len(field.directions)==31
    assert field.pending_boundaries is None
    dimensions=boundary_dimensions(model)
    restored=boundaries.restore(boundaries.metadata(field.boundaries,field.version),
        dict(boundaries.tensors(field.boundaries)),field.version,dimensions)
    embedding=np.array(model.model.embed_tokens(mx.arange(64)).astype(mx.float32))
    readout=PartitionTokenReadout.from_embeddings(embedding)
    engine=SimpleNamespace(model=model,field=field)
    adapter=Qwen35ReverseAdapter(engine,restored,readout)
    result=reconstruct_with_adapter(adapter,restored,restored,field.entries,str)
    assert result['method']=='approximate_reverse_propagation'
    assert result['estimated_token_ids']
    assert all(0 <= token < 64 for token in result['estimated_token_ids'])
    assert not field.capture


def test_disabled_capture_does_not_store_boundaries():
    model,field=tiny();field.config.enabled=False
    cache=[ArraysCache(size=2) if layer.is_linear else RotatingKVCache(16) for layer in model.layers]
    output=model(mx.array([[1]]),cache=cache);mx.eval(output);field.observe('user')
    assert not field.boundaries and not field.boundary_captured


def test_boundary_subclass_preserves_parameter_paths_and_forward_values():
    from mlx.utils import tree_flatten
    args=TextModelArgs(hidden_size=64,intermediate_size=128,num_hidden_layers=4,
        num_attention_heads=2,num_key_value_heads=1,head_dim=32,vocab_size=64,
        linear_num_value_heads=2,linear_num_key_heads=1,linear_key_head_dim=32,linear_value_head_dim=32)
    model=TextModel(args)
    before=dict(tree_flatten(model.parameters()))
    x=mx.array([[1]])
    output=model(x);mx.eval(output)
    field=Field([],FieldConfig(enabled=False))
    install_boundaries(model,field.boundary_tap)
    after=dict(tree_flatten(model.parameters()))
    actual=model(x);mx.eval(actual)
    assert before.keys()==after.keys()
    np.testing.assert_array_equal(np.asarray(output),np.asarray(actual))


def test_real_attention_head_size_reference_forward_and_vjp():
    from mlx.utils import tree_flatten
    from mlx_lm.models.qwen3_next import Qwen3NextAttention
    from mlx_lm.models.cache import KVCache
    from activation_field.adapters.qwen35_reverse import _install_reverse_attention
    args=TextModelArgs(hidden_size=64,intermediate_size=128,num_hidden_layers=4,
        num_attention_heads=16,num_key_value_heads=4,head_dim=256,vocab_size=64)
    attention=Qwen3NextAttention(args);nn.quantize(attention,group_size=64,bits=4)
    x=mx.random.normal((1,1,64));keys=mx.random.normal((1,4,1,256));values=mx.random.normal((1,4,1,256))
    def cached():
        cache=KVCache();cache.update_and_fetch(keys,values);return cache
    before=dict(tree_flatten(attention.parameters()))
    expected=attention(x,cache=cached());mx.eval(expected)
    original=Qwen3NextAttention.__call__
    _install_reverse_attention(attention)
    actual=attention(x,cache=cached());mx.eval(actual)
    np.testing.assert_allclose(np.asarray(actual),np.asarray(expected),rtol=2e-5,atol=2e-5)
    assert before.keys()==dict(tree_flatten(attention.parameters())).keys()
    assert Qwen3NextAttention.__call__ is original
    _,gradients=mx.vjp(lambda z:attention(z,cache=cached()),[x],[mx.ones_like(actual)])
    mx.eval(gradients)
    assert gradients[0].shape==x.shape
    assert bool(mx.all(mx.isfinite(gradients[0])).item())


def test_debug_preserves_native_reverse_and_observes_conditioning():
    from activation_field.reverse_debug import ReverseDebug
    model,field=tiny()
    cache=[ArraysCache(size=2) if layer.is_linear else RotatingKVCache(16) for layer in model.layers]
    for token in (1,2,3):
        output=model(mx.array([[token]]),cache=cache);mx.eval(output)
        field.observe('user')
    field.commit()
    # Nonzero accumulated write moments exercise the existing conditioning
    # arithmetic. They are a synthetic fixture, not a model-quality claim.
    for record in field.directions.values():
        record.arrays['write_mean']=mx.ones_like(record.arrays['write_mean'])*.01
    embedding=np.array(model.model.embed_tokens(mx.arange(64)).astype(mx.float32))
    readout=PartitionTokenReadout.from_embeddings(embedding)
    engine=SimpleNamespace(model=model,field=field)
    restored=boundaries.restore(boundaries.metadata(field.boundaries,field.version),
        dict(boundaries.tensors(field.boundaries)),field.version,boundary_dimensions(model))
    plain=Qwen35ReverseAdapter(engine,restored,readout)
    baseline=reconstruct_with_adapter(plain,restored,restored,field.entries,str)
    debug=ReverseDebug()
    traced=Qwen35ReverseAdapter(engine,restored,readout,debug=debug)
    actual=reconstruct_with_adapter(traced,restored,restored,field.entries,str,debug=debug)
    report=actual.pop('debug')
    assert actual==baseline
    assert len(report['steps'])==len(actual['estimated_token_ids'])
    for index in range(4):
        name='layer.'+str(index)
        assert name+'.incoming' in report['signal_names']
        assert name+'.conditioned' in report['signal_names']
        assert name+'.pullback' in report['signal_names']
    assert len(report['write_directions'])==31
    for path,record in field.directions.items():
        for stage in ('incoming','conditioned','pullback','input_boundary','output_boundary'):
            assert path+'.'+stage in report['signal_names']
        assert report['write_directions'][path]['combined_write_norm']==pytest.approx(
            .01*np.sqrt(record.arrays['write_mean'].size),rel=1e-6)



def test_debug_off_keeps_one_argument_readout_protocol():
    class PlainReadout:
        def readout(self,value):return 17
    adapter=SimpleNamespace(debug=None,token_readout=PlainReadout())
    assert Qwen35ReverseAdapter.readout(adapter,{'state.0':np.ones(2)},None)==17
