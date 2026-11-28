import numpy as np
import pytest
from activation_field.checkpoint import Vault


def test_authenticated_roundtrip(tmp_path):
    v=Vault(tmp_path);a=np.arange(1000,dtype=np.uint32).reshape(10,100)
    receipt=v.save('checkpoint',{'baseline_hash':'abc'},[('tensor',a),('empty',np.array([],np.float32))])
    assert receipt['baseline_hash']=='abc'
    got=dict(v.arrays('checkpoint'));assert np.array_equal(got['tensor'],a)
    shard=next((tmp_path/'checkpoint').glob('*.bin'));data=shard.read_bytes();shard.write_bytes(data[:-1]+bytes([data[-1]^1]))
    with pytest.raises(Exception):list(v.arrays('checkpoint'))


def test_no_partial_publish(tmp_path):
    v=Vault(tmp_path)
    with pytest.raises(ValueError):v.save('bad',{'baseline_hash':'abc'},[('x',np.array([float('nan')]))])
    assert not (tmp_path/'bad').exists()
