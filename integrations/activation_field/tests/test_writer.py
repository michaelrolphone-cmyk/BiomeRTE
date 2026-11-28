import numpy as np
import pytest
from activation_field.quantized_writer import pack,unpack,reference_write
from activation_field.config import FieldConfig


def fixture():
    rng=np.random.default_rng(2)
    q=rng.integers(1,15,(4,128),dtype=np.uint32)
    s=np.array([[.01,-.03]]*4,np.float32);b=np.array([[-.05,.2]]*4,np.float32)
    x=rng.normal(size=128).astype(np.float32);x/=np.linalg.norm(x)
    return pack(q),s,b,x,np.array([3,-3,2,0],np.float32),np.array([1,1,0,0],bool)


def test_roundtrip_and_noop():
    p,s,b,x,z,g=fixture()
    assert np.array_equal(pack(unpack(p)),p)
    def forbidden():raise AssertionError("zero pull read anchor")
    out,stats=reference_write(p,s,b,x,z,g,eta=0,role_scale=1,step_fraction_max=.001,seed=9,anchor=forbidden)
    assert np.array_equal(out,p);assert stats['changed_codes']==0


def test_frozen_rows_and_groups():
    p,s,b,x,z,g=fixture();s[0,0]=0
    out,_=reference_write(p,s,b,x,z,g,eta=.5,role_scale=1,step_fraction_max=1,seed=9)
    assert np.array_equal(out[2:],p[2:]);assert np.array_equal(out[0,:8],p[0,:8]);assert not np.array_equal(out[1],p[1])


def test_inactive_decay_and_tile_invariance():
    p,s,b,x,z,g=fixture();anchor=pack(np.full((4,128),7,np.uint32));g[:]=0
    args=dict(eta=0,role_scale=1,step_fraction_max=.001,seed=9,baseline_pull=1,anchor=anchor)
    a,_=reference_write(p,s,b,x,z,g,**args,tile_rows=1)
    c,_=reference_write(p,s,b,x,z,g,**args,tile_rows=4)
    assert np.array_equal(a,c);assert not np.array_equal(a,p)


@pytest.mark.parametrize('bad',[{'eta':float('nan')},{'baseline_pull':-1},{'unknown':1},{'commit_every_tokens':0},{'enabled':1}])
def test_config_rejects_invalid(bad):
    with pytest.raises(ValueError):FieldConfig.parse(bad)


def test_metal_matches_reference():
    mx=pytest.importorskip('mlx.core')
    if not mx.metal.is_available():pytest.skip('Metal backend unavailable; CPU MLX cannot run Metal kernels')
    from activation_field.kernels.field_write import write,row_norm
    p,s,b,x,z,g=fixture()
    arrays=[mx.array(a) for a in (p,s,b,x,z,g)]
    norms=row_norm(*arrays[:3]);mx.eval(norms)
    for pull in [0,1]:
        anchor=pack(np.full((4,128),7,np.uint32))
        kwargs=dict(eta=.005,role_scale=1,step_fraction_max=.01,seed=9,baseline_pull=pull)
        expected,_=reference_write(p,s,b,x,z,g,anchor=anchor,**kwargs)
        actual=write(*arrays,norms,anchor=mx.array(anchor),**kwargs);mx.eval(actual)
        assert np.array_equal(np.array(actual),expected)


def test_metal_audit_matches_measured_reference():
    mx=pytest.importorskip('mlx.core')
    if not mx.metal.is_available():pytest.skip('Metal backend unavailable; CPU MLX cannot run Metal kernels')
    from activation_field.kernels.field_write import write,row_norm,audit
    p,s,b,x,z,g=fixture()
    arrays=[mx.array(a) for a in (p,s,b,x,z,g)]
    norm=row_norm(*arrays[:3])
    for pull in (0.,1.):
        anchor=pack(np.full((4,128),7,np.uint32))
        kwargs=dict(eta=.005,role_scale=1,step_fraction_max=.01,seed=9,baseline_pull=pull)
        expected,stats=reference_write(p,s,b,x,z,g,anchor=anchor,**kwargs)
        actual=write(*arrays,norm,anchor=mx.array(anchor),**kwargs)
        metrics=audit(arrays[0],actual,*arrays[1:],norm,anchor=mx.array(anchor),**kwargs)
        mx.eval(actual,metrics)
        summed=np.array(metrics).sum(axis=0)
        assert summed[0]==sum(g)
        assert summed[1]==np.any(expected!=p,axis=1).sum()
        for i,name in enumerate(('changed_codes','candidate_codes','saturated_codes','proposal_squared_norm','realized_squared_norm','quantization_squared_error'),2):
            assert np.isclose(summed[i],stats[name],rtol=5e-4,atol=1e-5),(name,summed[i],stats[name])

