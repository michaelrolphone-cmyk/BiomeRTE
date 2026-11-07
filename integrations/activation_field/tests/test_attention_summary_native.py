"""Actual MLX cache and Qwen machinery, runnable on CPU or Apple Silicon."""
import numpy as np
import pytest
mx=pytest.importorskip('mlx.core')
nn=pytest.importorskip('mlx.nn')
from mlx_lm.models.cache import KVCache, RotatingKVCache, ArraysCache
from mlx_lm.models.qwen3_next import Qwen3NextAttention
from mlx_lm.models.qwen3_5 import TextModelArgs
from activation_field.attention_summary import (CROSS,MASS,paired_moments,paired_attention,
    prepare_context,finish_context)
from activation_field.boundaries import BoundaryRecord
from activation_field.adapters.qwen35_reverse import PairedSummaryCache,_install_reverse_attention
from test_attention_summary import dense_kernel,descriptor


@pytest.mark.parametrize('capacity,keep', [(1,0),(2,0),(5,0),(7,2),(4,3),(None,0)])
def test_incremental_survivor_summary_matches_real_cache(capacity,keep):
    cache=RotatingKVCache(capacity,keep) if capacity else KVCache()
    rng=np.random.default_rng(17)
    shape=(1,2,1,4)
    for step in range(80):
        clone=RotatingKVCache(capacity,keep) if capacity else KVCache()
        clone.state=cache.state
        summary,info=prepare_context(cache,2,4,xp=mx)
        zeros=mx.zeros(shape)
        actual_k,actual_v=clone.update_and_fetch(zeros,zeros)
        expected=paired_moments(actual_k,actual_v,xp=mx)
        removed=paired_moments(zeros,zeros,xp=mx)
        for key in summary:
            np.testing.assert_allclose(np.asarray(summary[key]),np.asarray(expected[key]-removed[key]),rtol=2e-5,atol=2e-5)
            assert summary[key].shape==((1,2,4,4) if key==CROSS else (1,2,4))
        assert info['query_position']==step
        assert info['past_count']==(min(step,capacity-1) if capacity else step)
        k=mx.array(rng.normal(size=shape).astype(np.float32))
        v=mx.array(rng.normal(size=shape).astype(np.float32))
        cache.update_and_fetch(k,v)
        finish_context(cache,summary,info,xp=mx)
        if step in (17,29):cache._boundary_attention_summary=None


@pytest.mark.parametrize('keep',[0,2])
def test_oversized_prefill_trim_before_one_token_capture(keep):
    cache=RotatingKVCache(5,keep)
    for count in (3,11):
        cache.update_and_fetch(mx.random.normal((1,1,count,4)),mx.random.normal((1,1,count,4)))
    assert cache.keys.shape[2]>cache.max_size
    summary,info=prepare_context(cache,1,4,xp=mx)
    clone=RotatingKVCache(5,keep);clone.state=cache.state
    zero=mx.zeros((1,1,1,4));k,v=clone.update_and_fetch(zero,zero)
    expected=paired_moments(k,v,xp=mx);removed=paired_moments(zero,zero,xp=mx)
    for key in summary:
        np.testing.assert_allclose(np.asarray(summary[key]),np.asarray(expected[key]-removed[key]),rtol=2e-5,atol=2e-5)
    assert info['past_count']==4
    assert info['tail_start']==14-(4-keep)


@pytest.mark.parametrize('dimension,heads,kv_heads',[(4,2,1),(256,16,4)])
def test_summary_vjp_matches_dense_kernel_for_q_and_current_kv(dimension,heads,kv_heads):
    rng=np.random.default_rng(42)
    hist_k=mx.array(rng.normal(size=(1,kv_heads,9,dimension)).astype(np.float32))
    hist_v=mx.array(rng.normal(size=hist_k.shape).astype(np.float32))
    summary=paired_moments(hist_k,hist_v,xp=mx);mx.eval(*summary.values())
    q=mx.array(rng.normal(size=(1,heads,1,dimension)).astype(np.float32))
    k=mx.array(rng.normal(size=(1,kv_heads,1,dimension)).astype(np.float32))
    v=mx.array(rng.normal(size=k.shape).astype(np.float32))
    incoming=mx.array(rng.normal(size=q.shape).astype(np.float32))
    def summarized(q,k,v):return paired_attention(q,k,v,summary,xp=mx)
    def dense(q,k,v):return dense_kernel(q,mx.concatenate((hist_k,k),axis=2),mx.concatenate((hist_v,v),axis=2),xp=mx)
    a,ga=mx.vjp(summarized,[q,k,v],[incoming])
    b,gb=mx.vjp(dense,[q,k,v],[incoming]);mx.eval(a,b,ga,gb)
    np.testing.assert_allclose(np.asarray(a[0]),np.asarray(b[0]),rtol=2e-5,atol=2e-6)
    for actual,expected in zip(ga,gb):
        np.testing.assert_allclose(np.asarray(actual),np.asarray(expected),rtol=2e-4,atol=2e-6)
        assert float(mx.linalg.norm(actual))>0


def test_private_qwen_attention_uses_exact_absolute_rope_position():
    args=TextModelArgs(hidden_size=64,intermediate_size=128,num_hidden_layers=4,
        num_attention_heads=2,num_key_value_heads=1,head_dim=32,vocab_size=64)
    attention=Qwen3NextAttention(args)
    position=2**24+19
    arrays={'context.'+CROSS:np.ones((1,1,32,32),np.float32),
            'context.'+MASS:np.ones((1,1,32),np.float32)}
    record=BoundaryRecord(1.,1,0.,arrays,descriptor(position,8))
    seen=[]
    class RecordRope(nn.Module):
        def __init__(self,rope):super().__init__();self.original=rope
        def __call__(self,x,offset=0):
            seen.append(offset)
            return self.original(x,offset=offset)
    attention.rope=RecordRope(attention.rope)
    _install_reverse_attention(attention)
    x=mx.random.normal((1,1,64))
    output,gradient=mx.vjp(lambda x:attention(x,cache=PairedSummaryCache(record)),[x],[mx.ones_like(x)])
    mx.eval(output,gradient)
    assert seen==[position,position]
    assert all(type(offset) is int for offset in seen)
    assert np.isfinite(np.asarray(gradient[0])).all()


def test_enabled_capture_preserves_native_forward_through_ring_evictions():
    from test_reverse_native import tiny
    model,field=tiny()
    expected_cache=[ArraysCache(size=2) if layer.is_linear else RotatingKVCache(4,1) for layer in model.layers]
    actual_cache=[ArraysCache(size=2) if layer.is_linear else RotatingKVCache(4,1) for layer in model.layers]
    for token in range(1,16):
        field.config.enabled=False
        expected=model(mx.array([[token]]),cache=expected_cache);mx.eval(expected)
        field.config.enabled=True
        actual=model(mx.array([[token]]),cache=actual_cache);mx.eval(actual)
        field.observe('user')
        np.testing.assert_array_equal(np.asarray(actual),np.asarray(expected))
    field.commit()
    record=field.boundaries['layer.3']
    assert record.context_metadata['query_position']==14
    assert record.context_metadata['past_count']==3
    assert record.context_metadata['prefix_count']==1


def test_legacy_mean_cell_records_still_reverse_with_actual_qwen():
    from types import SimpleNamespace
    from test_reverse_native import tiny
    from activation_field.adapters.qwen35_reverse import Qwen35ReverseAdapter
    from activation_field.inverse_runtime import reconstruct_with_adapter
    model,field=tiny()
    cache=[ArraysCache(size=2) if layer.is_linear else RotatingKVCache(4) for layer in model.layers]
    for token in (1,2,3):
        output=model(mx.array([[token]]),cache=cache);mx.eval(output);field.observe('user')
    field.commit()
    records={name:BoundaryRecord(r.mass,r.samples,r.motion,{k:np.asarray(v) for k,v in r.arrays.items()},r.context_metadata)
             for name,r in field.boundaries.items()}
    record=records['layer.3'];arrays={k:v for k,v in record.arrays.items() if not k.startswith('context.')}
    k,v=cache[3].keys_and_values()
    arrays.update({'context.key_mean':mx.mean(k,axis=2,keepdims=True),
                   'context.value_mean':mx.mean(v,axis=2,keepdims=True)})
    records['layer.3']=BoundaryRecord(record.mass,record.samples,record.motion,arrays)
    adapter=Qwen35ReverseAdapter(SimpleNamespace(model=model,field=field),records,SimpleNamespace(readout=lambda x:7))
    result=reconstruct_with_adapter(adapter,records,records,field.entries,str)
    assert result['estimated_token_ids'] and set(result['estimated_token_ids'])=={7}


def test_zero_write_footer_keeps_final_cache_position_without_new_samples():
    from test_reverse_native import tiny
    model,field=tiny()
    cache=[ArraysCache(size=2) if layer.is_linear else RotatingKVCache(4) for layer in model.layers]
    for token in (1,2):
        output=model(mx.array([[token]]),cache=cache);mx.eval(output);field.observe('user')
    field.commit()
    before=field.boundaries['layer.3']
    for token in (3,4,5):
        output=model(mx.array([[token]]),cache=cache);mx.eval(output);field.observe('user',template=True)
    field.commit()
    after=field.boundaries['layer.3']
    assert after.context_metadata['query_position']==4
    assert after.context_metadata['past_count']==3
    assert (after.mass,after.samples,after.motion)==(before.mass,before.samples,before.motion)
    for key in ('input_mean','output_mean','current_output','backward_flow'):
        np.testing.assert_array_equal(np.asarray(after.arrays[key]),np.asarray(before.arrays[key]))


def test_saved_boundary_hydrates_exact_tracker_for_checkpoint_continuation():
    from activation_field.attention_summary import restore_context_tracker
    from activation_field import boundaries
    rng=np.random.default_rng(88)
    cache=RotatingKVCache(64,2)
    last=None
    for step in range(110):
        summary,info=prepare_context(cache,2,32,xp=mx)
        k=mx.array(rng.normal(size=(1,2,1,32)).astype(np.float32))
        v=mx.array(rng.normal(size=k.shape).astype(np.float32))
        cache.update_and_fetch(k,v);finish_context(cache,summary,info,xp=mx)
        last=boundaries.accumulate(last,np.ones(8),np.ones(8),summary,1,context_metadata=info)
    saved=boundaries.restore(boundaries.metadata({'layer.0':last},0),
                            dict(boundaries.tensors({'layer.0':last})),0,
                            {'layer.0':(8,8,{CROSS:(1,2,32,32),MASS:(1,2,32)})})['layer.0']
    restored=RotatingKVCache(64,2);restored.state=cache.state
    assert restore_context_tracker(restored,saved.arrays,saved.context_metadata,xp=mx,confirmed_current=True)
    for step in range(150):
        a,ai=prepare_context(cache,2,32,xp=mx)
        b,bi=prepare_context(restored,2,32,xp=mx)
        assert ai==bi
        for key in a:np.testing.assert_array_equal(np.asarray(a[key]),np.asarray(b[key]))
        k=mx.array(rng.normal(size=(1,2,1,32)).astype(np.float32))
        v=mx.array(rng.normal(size=k.shape).astype(np.float32))
        cache.update_and_fetch(k,v);finish_context(cache,a,ai,xp=mx)
        restored.update_and_fetch(k,v);finish_context(restored,b,bi,xp=mx)
    assert not restore_context_tracker(restored,saved.arrays,saved.context_metadata,xp=mx)


def test_capture_resumes_after_unobserved_native_updates():
    rng=np.random.default_rng(123)
    cache=RotatingKVCache(8,2)
    for step in range(40):
        if step not in range(11,25):summary,info=prepare_context(cache,2,16,xp=mx)
        k=mx.array(rng.normal(size=(1,2,1,16)).astype(np.float32))
        v=mx.array(rng.normal(size=k.shape).astype(np.float32))
        cache.update_and_fetch(k,v)
        if step not in range(11,25):finish_context(cache,summary,info,xp=mx)
    summary,info=prepare_context(cache,2,16,xp=mx)
    fresh=RotatingKVCache(8,2);fresh.state=cache.state
    expected,expected_info=prepare_context(fresh,2,16,xp=mx)
    assert info==expected_info
    for key in summary:np.testing.assert_allclose(np.asarray(summary[key]),np.asarray(expected[key]),rtol=2e-5,atol=2e-5)


def test_reset_and_unobserved_history_cannot_hydrate_old_matching_offset():
    from activation_field.attention_summary import restore_context_tracker,context_tracker_is_current
    old=RotatingKVCache(8)
    for step in range(3):
        summary,info=prepare_context(old,1,4,xp=mx)
        old.update_and_fetch(mx.ones((1,1,1,4)),mx.ones((1,1,1,4)))
        finish_context(old,summary,info,xp=mx)
    assert context_tracker_is_current(old,info)
    arrays={'context.'+k:v for k,v in summary.items()}
    replacement=RotatingKVCache(8)
    for step in range(3):
        replacement.update_and_fetch(mx.ones((1,1,1,4)),mx.ones((1,1,1,4))*100)
    assert old.offset==replacement.offset
    # This is what complete-checkpoint state() records after reset followed by
    # disabled/read-only ingestion: no fresh tracker for the replacement cache.
    current=context_tracker_is_current(replacement,info)
    assert not current
    restored=RotatingKVCache(8);restored.state=replacement.state
    assert not restore_context_tracker(restored,arrays,info,xp=mx,confirmed_current=current)
    actual,_=prepare_context(restored,1,4,xp=mx)
    expected=paired_moments(*replacement.keys_and_values(),xp=mx)
    for key in actual:np.testing.assert_array_equal(np.asarray(actual[key]),np.asarray(expected[key]))


def test_actual_engine_checkpoint_validates_and_restores_tracker_freshness(tmp_path):
    from test_reverse_native import tiny
    from activation_field.runtime import Engine
    from activation_field.trajectory import Trajectory
    from activation_field.checkpoint import Vault
    from activation_field.attention_summary import context_tracker_is_current
    model,field=tiny();field.config.context_length=4
    engine=object.__new__(Engine)
    engine.model=model;engine.field=field;engine.config=field.config
    engine.manifest={'baseline_hash':'test-baseline','versions':{}}
    engine.cache=[ArraysCache(size=2) if layer.is_linear else RotatingKVCache(4) for layer in model.layers]
    engine.trajectory=Trajectory(128,32,False)
    engine.consumed_tokens=engine.sample_counter=engine.native_tokens=0
    engine.prefix_hashes=[];engine.last_logits=None
    engine.input_stream_digest='0'*64;engine.input_digest_origin='complete_token_chain'
    for token in range(1,10):
        output=model(mx.array([[token]]),cache=engine.cache);mx.eval(output);field.observe('user')
        engine.native_tokens+=1
    meta,tensors=engine.state()
    assert meta['attention_tracker_current']==[3]
    expected,expected_info=prepare_context(engine.cache[3],1,32,xp=mx)
    vault=Vault(tmp_path/'checkpoint');vault.save('saved',meta,tensors)
    engine.restore(vault,'saved')
    record=engine.field.boundaries['layer.3']
    assert context_tracker_is_current(engine.cache[3],record.context_metadata)
    actual,info=prepare_context(engine.cache[3],1,32,xp=mx)
    assert info==expected_info
    for key in actual:np.testing.assert_array_equal(np.asarray(actual[key]),np.asarray(expected[key]))
    # Reset the ordinary cache while retaining weighted boundaries, then ingest
    # the same number of unobserved tokens so the stale offset happens to match.
    field.config.enabled=False
    engine.cache=[ArraysCache(size=2) if layer.is_linear else RotatingKVCache(4) for layer in model.layers]
    for token in range(10,19):
        output=model(mx.array([[token]]),cache=engine.cache);mx.eval(output);field.observe('user')
    meta,tensors=engine.state()
    assert meta['attention_tracker_current']==[]
    expected,expected_info=prepare_context(engine.cache[3],1,32,xp=mx)
    vault.save('unobserved',meta,tensors);engine.restore(vault,'unobserved')
    assert not context_tracker_is_current(engine.cache[3],record.context_metadata)
    actual,info=prepare_context(engine.cache[3],1,32,xp=mx)
    assert info==expected_info
    for key in actual:np.testing.assert_array_equal(np.asarray(actual[key]),np.asarray(expected[key]))
    engine.trajectory.close()
