"""Analytic and state-format checks; no pretrained model or semantic claims."""
import copy
import json
import numpy as np
import pytest
from activation_field.attention_summary import (
    CROSS, MASS, FEATURE_MAP, positive_features, paired_moments,
    paired_attention, prepare_context, finish_context, validate_descriptor)
from activation_field import boundaries


def descriptor(position=4, capacity=8, keep=0):
    count = min(position, capacity-1) if capacity is not None else position
    prefix = min(position, keep)
    return dict(kind='paired_kernel', feature_map=FEATURE_MAP,
                query_position=position, cache_kind='rotating' if capacity else 'growing',
                cache_capacity=capacity, cache_keep=keep, past_count=count,
                prefix_count=prefix, tail_start=position-(count-prefix), tail_end=position)


def dense_kernel(q, k, v, xp=np):
    b, h, length, d = q.shape
    heads = k.shape[1]
    q = positive_features(q, xp).reshape(b, heads, h//heads, length, d)
    k = positive_features(k, xp)[:, :, None, :, :]
    scores = q @ xp.swapaxes(k, -1, -2)
    return ((scores / xp.sum(scores, axis=-1, keepdims=True)) @
            v[:, :, None, :, :]).reshape(b, h, length, v.shape[-1])


@pytest.mark.parametrize('history', [0,1,8,129])
@pytest.mark.parametrize('heads,kv_heads,dimension', [(2,1,4),(16,4,256)])
def test_paired_response_equals_dense_positive_kernel(history, heads, kv_heads, dimension):
    rng=np.random.default_rng(83)
    q=rng.normal(size=(1,heads,1,dimension)).astype(np.float32)
    k=rng.normal(size=(1,kv_heads,history+1,dimension)).astype(np.float32)
    v=rng.normal(size=k.shape).astype(np.float32)
    summary=paired_moments(k[:,:,:history],v[:,:,:history])
    actual=paired_attention(q,k[:,:,history:],v[:,:,history:],summary)
    np.testing.assert_allclose(actual,dense_kernel(q,k,v),rtol=3e-5,atol=2e-6)


def test_pairing_survives_when_separate_means_are_identical():
    keys=np.array([[[[-3.,2.],[3.,-2.]]]],np.float32)
    values=np.array([[[[-4.,1.],[4.,-1.]]]],np.float32)
    swapped=values[:,:,::-1,:]
    np.testing.assert_array_equal(keys.mean(2),keys.mean(2))
    np.testing.assert_array_equal(values.mean(2),swapped.mean(2))
    q=np.array([[[[3.,-2.]]]],np.float32)
    current_k=np.zeros((1,1,1,2),np.float32)
    current_v=np.zeros_like(current_k)
    a=paired_attention(q,current_k,current_v,paired_moments(keys,values))
    b=paired_attention(q,current_k,current_v,paired_moments(keys,swapped))
    assert np.linalg.norm(a-b)>1


def test_positive_features_and_response_are_finite_at_extremes():
    for sign in (-1,1):
        q=np.full((1,2,1,32),sign*1e4,np.float32)
        k=np.full((1,1,7,32),-sign*1e4,np.float32)
        v=np.full_like(k,17)
        features=positive_features(k)
        assert np.isfinite(features).all() and (features>0).all()
        response=paired_attention(q,k[:,:,:1],v[:,:,:1],paired_moments(k[:,:,1:],v[:,:,1:]))
        np.testing.assert_allclose(response,17,atol=1e-4)


def test_schema2_roundtrip_fixed_shapes_and_exact_large_position():
    position=2**40+13
    info=descriptor(position,16,2)
    context={CROSS:np.ones((1,2,4,4),np.float32), MASS:np.ones((1,2,4),np.float32)}
    r=boundaries.accumulate(None,np.ones(8),np.ones(8),context,1,context_metadata=info)
    m=json.loads(json.dumps(boundaries.metadata({'layer.0':r},7)))
    assert m['schema_version']==2
    loaded=boundaries.restore(m,dict(boundaries.tensors({'layer.0':r})),7,
                              {'layer.0':(8,8,{k:v.shape for k,v in context.items()})})
    assert loaded['layer.0'].context_metadata==info
    assert loaded['layer.0'].context_metadata['query_position']==position
    assert all(v.dtype==np.float32 for v in loaded['layer.0'].arrays.values())
    nbytes=sum(a.nbytes for a in r.arrays.values())
    for i in range(60):
        r=boundaries.accumulate(r,np.ones(8),np.ones(8),context,1,context_metadata=descriptor(position+i,16,2))
        assert sum(a.nbytes for a in r.arrays.values())==nbytes


@pytest.mark.parametrize('change', [
    {'query_position':True}, {'past_count':-1}, {'prefix_count':2},
    {'tail_start':99}, {'tail_end':3}, {'cache_keep':8},
    {'cache_capacity':0}, {'cache_capacity':8.0}, {'feature_map':'unknown'},
    {'extra':'unrecognized'}, {'cache_kind':'unknown'}])
def test_descriptor_rejects_inconsistent_or_unknown_data(change):
    info=descriptor();info.update(change)
    with pytest.raises(ValueError):validate_descriptor(info)


def test_legacy_v1_and_resaved_v2_remain_mean_cells():
    context=dict(key_mean=np.ones((1,1,1,4),np.float32),
                 value_mean=np.ones((1,1,1,4),np.float32))
    record=boundaries.accumulate(None,np.ones(8),np.ones(8),context,1)
    dims={'layer.0':(8,8,{CROSS:(1,1,4,4),MASS:(1,1,4)})}
    meta=boundaries.metadata({'layer.0':record},0)
    data=dict(boundaries.tensors({'layer.0':record}))
    for version in (1,2):
        m=copy.deepcopy(meta);m['schema_version']=version
        if version==1:m['operations']['layer.0'].pop('context')
        loaded=boundaries.restore(m,data,0,dims)['layer.0']
        assert loaded.context_metadata=={}
        assert 'context.key_mean' in loaded.arrays
        assert 'context.'+CROSS not in loaded.arrays


def test_paired_checkpoint_requires_metadata_and_valid_moments():
    context={CROSS:np.zeros((1,1,4,4),np.float32), MASS:np.zeros((1,1,4),np.float32)}
    record=boundaries.accumulate(None,np.ones(8),np.ones(8),context,1,context_metadata=descriptor(0))
    meta=boundaries.metadata({'layer.0':record},0)
    data=dict(boundaries.tensors({'layer.0':record}))
    dims={'layer.0':(8,8,{k:v.shape for k,v in context.items()})}
    boundaries.restore(meta,data,0,dims)
    bad=copy.deepcopy(meta);bad['operations']['layer.0']['context']={}
    with pytest.raises(ValueError):boundaries.restore(bad,data,0,dims)
    for value in (np.ones((1,1,4),np.float32),np.zeros((1,1,4),np.float64),np.full((1,1,4),np.nan,np.float32)):
        bad=dict(data);bad['boundary.layer.0.context.'+MASS]=value
        with pytest.raises(ValueError):boundaries.restore(meta,bad,0,dims)


def test_snapshot_preserves_paired_state_without_opening_cache(tmp_path):
    from activation_field.checkpoint import Vault
    from activation_field.context_reconstruction.snapshot import snapshot
    context={CROSS:np.ones((1,1,4,4),np.float32),MASS:np.ones((1,1,4),np.float32)}
    record=boundaries.accumulate(None,np.ones(8),np.ones(8),context,1,context_metadata=descriptor(2**30))
    source=Vault(tmp_path/'source');target=Vault(tmp_path/'target')
    meta=dict(baseline_hash='base',tensor_map_version=1,kind='complete',config={},
              boundaries=boundaries.metadata({'layer.0':record},0),
              cache={'secret':'native cache tree'},prefix_hashes=['private'],last_logits='private')
    source.save('old',meta,[*boundaries.tensors({'layer.0':record}),('cache.0',np.ones((10,4),np.float32))])
    m=source.manifest('old')
    for t in m['tensors']:
        if t['name']=='cache.0':
            for chunk in t['chunks']:(source.root/'old'/chunk['file']).write_bytes(b'not readable')
    snapshot(source,'old',target)
    result=target.manifest('source')
    assert result['metadata']['snapshot_schema_version']==2
    assert result['metadata']['boundaries']==meta['boundaries']
    assert not {'cache','last_logits','prefix_hashes'} & set(result['metadata'])
    assert all(name.startswith('boundary.') for name,_ in target.arrays('source'))


def test_zero_mass_refreshes_only_final_context_without_weighted_movement():
    context={CROSS:np.ones((1,1,4,4),np.float32),MASS:np.ones((1,1,4),np.float32)}
    r=boundaries.accumulate(None,np.ones(8),np.ones(8),context,1,context_metadata=descriptor(1))
    refreshed=boundaries.accumulate(r,np.zeros(8),np.zeros(8),{k:v*2 for k,v in context.items()},0,
                                   context_metadata=descriptor(2))
    assert refreshed.context_metadata['query_position']==2
    assert (refreshed.mass,refreshed.samples,refreshed.motion)==(r.mass,r.samples,r.motion)
    for key,value in r.arrays.items():
        np.testing.assert_array_equal(refreshed.arrays[key],value*2 if key.startswith('context.') else value)
    assert boundaries.accumulate(None,np.zeros(8),np.zeros(8),context,0,context_metadata=descriptor(2)) is None
