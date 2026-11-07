"""Algebra/immutability checks, not semantic reconstruction claims."""
from copy import deepcopy
import numpy as np
import pytest
from activation_field import boundaries, directions
from activation_field.attention_summary import CROSS, MASS
from activation_field.reverse_state import (ProjectionState, LayerState, ModelTrajectory,
    remove_paired_cell, reverse_conv_mean, regularized_predecessor)


def test_all_projection_moments_used_without_promoting_flow_or_write():
    record=None
    for x,y in (([1,0],[0,1]),([0,1],[1,0]),([1,1],[-1,1])):
        record=directions.accumulate(record,x,y,[1e-8,-1e-8],1)
    before=deepcopy(record.arrays)
    state=ProjectionState(record)
    np.testing.assert_allclose(state.input,directions.normalized(before['current_input']+before['input_mean']))
    np.testing.assert_allclose(state.output,directions.normalized(before['current_output']+before['output_mean']))
    for _ in range(record.samples-1):
        old_in,old_out=state.input.copy(),state.output.copy()
        state.advance()
        np.testing.assert_allclose(state.input,directions.normalized(old_in-before['input_flow']))
        np.testing.assert_allclose(state.output,directions.normalized(old_out-before['output_flow']))
    assert state.done
    np.testing.assert_array_equal(state.write,before['write_mean'])
    for k in before:np.testing.assert_array_equal(record.arrays[k],before[k])


@pytest.mark.parametrize('mass',[0.,.01,1.,100.])
def test_paired_removal_same_clipped_features_and_zero_rows(mass):
    features=np.array([[[[.5,2.]]]],np.float32)
    value=np.array([[[[-3.,2.]]]],np.float32)
    summary={MASS:np.array([[[mass,0.]]],np.float32),CROSS:np.zeros((1,1,2,2),np.float32)}
    old=deepcopy(summary)
    result=remove_paired_cell(summary,features,value)
    assert np.all(result[MASS]>=0)
    assert np.all(result[MASS]<=old[MASS])
    removed=np.minimum(features.squeeze(-2),old[MASS])
    expected=-removed[...,None]*value.squeeze(-2)[...,None,:]
    expected=np.where(result[MASS][...,None]>0,expected,0)
    np.testing.assert_array_equal(result[CROSS],expected)
    for k in summary:np.testing.assert_array_equal(summary[k],old[k])


def test_regularized_predecessor_matches_one_ridge_gradient_step():
    current=np.array([2.,-3.],np.float32);prior=np.array([1.,5.],np.float32)
    operator=np.diag([.7,.2]).astype(np.float32);bias=np.array([3.,2.],np.float32)
    residual=current-(operator@current+bias)
    actual=regularized_predecessor(current,operator.T@residual,prior)
    gradient=operator.T@(operator@current+bias-current)+(current-prior)
    np.testing.assert_allclose(actual,current-.5*gradient)
    assert not np.allclose(actual,np.linalg.solve(operator,current-bias))


@pytest.mark.parametrize('slots',[0,1,2,3,8])
def test_convolution_aggregate_step(slots):
    current=np.array([[[2.,3.]]],np.float32);newest=np.array([[[5.,7.]]],np.float32)
    actual=reverse_conv_mean(current,newest,slots)
    if slots<=1:np.testing.assert_array_equal(actual,current)
    else:
        a=(slots-1)/slots
        np.testing.assert_allclose(actual,current+.5*a*(current-(a*current+newest/slots)))


def test_layer_and_projection_motion_outlive_stationary_terminal():
    terminal=boundaries.accumulate(None,[1.,0.],[1.,0.],{},1)
    moving=None;projection=None
    for angle in np.arange(5)*.2:
        value=np.array([np.cos(angle),np.sin(angle)],np.float32)
        moving=boundaries.accumulate(moving,value,value,{'recurrent':np.ones((2,2),np.float32)},1)
        projection=directions.accumulate(projection,value,value,np.zeros(2),1)
    source=deepcopy(moving.arrays)
    state=ModelTrajectory({'final_norm':terminal,'layer.0':moving},{'projection':projection})
    state.terminal.advance()
    assert state.terminal.done and not state.done
    initial=state.layers['layer.0'].input.copy()
    for _ in range(20):
        layer=state.layers['layer.0'];layer.advance(np.array([0.,1.],np.float32))
        state.projections['projection'].advance()
        if state.done:break
    assert state.done
    assert not np.array_equal(initial,layer.input)
    for key in source:np.testing.assert_array_equal(moving.arrays[key],source[key])


def test_rotating_preserved_prefix_is_not_removed_in_evicted_middle():
    record=boundaries.accumulate(None,[1,0],[1,0],{},1,
        context_metadata=dict(query_position=100,past_count=3,prefix_count=2,tail_start=99))
    state=LayerState(record)
    removed=[]
    while state.position:
        if state.has_predecessor_cell():
            removed.append(state.position-1);state.past_count-=1
        state.position-=1
    assert removed==[99,1,0]
    assert state.past_count==0 and not state.has_predecessor_cell()
