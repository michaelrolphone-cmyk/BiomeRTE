"""Tiny actual Qwen mechanics: coverage, evolving contexts and no mutation."""
from copy import deepcopy
from types import SimpleNamespace
import numpy as np
import pytest
mx=pytest.importorskip('mlx.core')
from mlx_lm.models.cache import ArraysCache,RotatingKVCache
from test_reverse_native import tiny
from activation_field import boundaries
from activation_field.adapters.qwen35 import boundary_dimensions
from activation_field.adapters.qwen35_reverse import Qwen35ReverseAdapter
from activation_field.reverse import reverse_operations,ReverseUnavailable
from activation_field.reverse_debug import ReverseDebug
from activation_field.attention_summary import CROSS,MASS


def captured(layers=4):
    model,field=tiny(layers)
    cache=[ArraysCache(size=2) if layer.is_linear else RotatingKVCache(4) for layer in model.layers]
    for token in (1,2,3,4):
        mx.eval(model(mx.array([[token]]),cache=cache));field.observe('user')
    field.commit()
    records=boundaries.restore(boundaries.metadata(field.boundaries,field.version),
        dict(boundaries.tensors(field.boundaries)),field.version,boundary_dimensions(model))
    return model,field,records


def test_all_248_projection_boundaries_writes_and_contexts_evolve():
    model,field,records=captured(32)
    before=deepcopy(records)
    weights=[(module,module.weight,module.scales,module.biases) for _,module,*_ in field.entries]
    debug=ReverseDebug()
    adapter=Qwen35ReverseAdapter(SimpleNamespace(model=model,field=field),records,
                               SimpleNamespace(readout=lambda value:7),debug=debug)
    trajectory=adapter.begin(records,records,field.entries)
    context_before={name:deepcopy(state.context) for name,state in trajectory.layers.items()}
    projection_before={path:(state.input.copy(),state.output.copy()) for path,state in trajectory.projections.items()}
    for _ in range(2):
        debug.begin_step()
        flow=reverse_operations(adapter.operations,adapter.signals(trajectory),records,
                                field.entries,adapter.counterparts,debug=debug)
        adapter.advance(trajectory,flow);debug.end_step(7)
    assert len(debug.write_directions)==248
    for path,state in trajectory.projections.items():
        assert debug.signal_summary[path+'.pullback']['observations']==2
        assert not np.array_equal(state.input,projection_before[path][0]) or not np.array_equal(state.output,projection_before[path][1])
    for name,state in trajectory.layers.items():
        keys=state.context.keys()
        assert all(np.isfinite(value).all() for value in state.context.values())
        if MASS in keys:
            assert state.position==records[name].context_metadata['query_position']-2
            assert np.all(state.context[MASS]<=context_before[name][MASS])
        else:
            for key in ('conv_mean','recurrent'):
                assert not np.array_equal(state.context[key],context_before[name][key])
        for key in before[name].arrays:np.testing.assert_array_equal(before[name].arrays[key],records[name].arrays[key])
    for module,w,s,b in weights:
        assert module.weight is w and module.scales is s and module.biases is b


def test_incomplete_projection_directions_fail_explicitly():
    model,field,records=captured()
    field.directions.pop(next(iter(field.directions)))
    with pytest.raises(ReverseUnavailable,match='Every executing projection'):
        Qwen35ReverseAdapter(SimpleNamespace(model=model,field=field),records,None)


def test_each_projection_write_changes_its_own_counterpart_before_transpose():
    from activation_field.adapters.reverse_projection import install_projection
    from activation_field.reverse_state import ProjectionState
    from activation_field.directions import accumulate
    from activation_field.adapters.qwen35 import Tap
    import mlx.nn as nn
    native=nn.QuantizedLinear(64,128,bias=False,group_size=64,bits=4)
    tap=Tap(native,'test',lambda *args:None)
    record=accumulate(None,np.ones(64),np.ones(128),np.linspace(-1e-5,1e-5,128),1)
    state=ProjectionState(record)
    install_projection(tap,lambda:state,'test',lambda:None)
    x=mx.ones((1,1,64));signal=mx.ones((1,1,128))
    _,actual=mx.vjp(tap,[x],[signal])
    _,expected=mx.vjp(native,[x],[signal+mx.array(state.write).reshape(1,1,-1)])
    mx.eval(actual,expected)
    np.testing.assert_allclose(np.asarray(actual[0]),np.asarray(expected[0]),rtol=1e-6,atol=1e-6)


def test_reinstall_does_not_stack_classes_or_keep_prior_trajectory_graphs():
    import gc
    import weakref
    model,field,records=captured()
    engine=SimpleNamespace(model=model,field=field)
    def classes():
        return [type(getattr(parent,name)) for _,_,parent,name,*_ in field.entries]+[
            type(layer.linear_attn if layer.is_linear else layer.self_attn) for layer in model.layers]
    original_lengths=[len(kind.__mro__) for kind in classes()]
    adapter=Qwen35ReverseAdapter(engine,records,None)
    trajectory=adapter.begin(records,records,field.entries)
    flow=reverse_operations(adapter.operations,adapter.signals(trajectory),records,field.entries,adapter.counterparts)
    adapter.advance(trajectory,flow)
    for state in trajectory.projections.values():
        assert not any(hasattr(state,k) for k in ('last_output','last_incoming','last_conditioned','last_pullback'))
    assert all(state.pending is None for state in trajectory.layers.values())
    assert all(layer.linear_attn._reverse_recurrence is None for layer in model.layers if layer.is_linear)
    reference=weakref.ref(adapter)
    replacement=Qwen35ReverseAdapter(engine,records,None)
    assert [len(kind.__mro__) for kind in classes()]==[n+1 for n in original_lengths]
    del adapter,trajectory;gc.collect()
    assert reference() is None
    assert replacement.engine is engine
