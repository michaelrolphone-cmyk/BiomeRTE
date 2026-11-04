import numpy as np
from activation_field.spectra import describe,dtw


def test_zero_graph_and_temporal_identity():
    result=describe(np.zeros((16,512)))
    assert result['isolated_nodes']==512
    assert np.max(np.abs(result['eigenvalues']))==0
    assert result['heat_trace']['1']==512
    a=np.arange(30).reshape(10,3)
    assert dtw(a,a)==0
