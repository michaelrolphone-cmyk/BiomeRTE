"""Synthetic mechanics tests, not model trials or recovery qualification."""
import numpy as np
import pytest

from activation_field.token_readout import PartitionTokenReadout


def test_branch_readout_emits_single_token():
    readout = PartitionTokenReadout.from_embeddings([[-2, 0], [2, 0]], [7, 19], normalize=False)
    assert readout.readout([-1, 0]) == 7
    assert readout([1, 0]) == 19
    assert readout([2, 0]) == 19
    assert type(readout([3, 5])) is int


def test_normalized_direction_readout():
    readout = PartitionTokenReadout([[5, 0], [0, 3]], [11, 13])
    assert readout([200, 0]) == 11
    assert readout([0, 200]) == 13


def test_duplicate_embedding_region_uses_deterministic_id():
    readout = PartitionTokenReadout([[1, 0], [1, 0]], [9, 4])
    assert readout([1, 0]) == 4
    assert len(readout._nodes) == 1


def test_readout_needs_only_tree_not_original_embedding_table():
    embeddings = np.array([[-1, 0], [1, 0]], dtype=np.float32)
    readout = PartitionTokenReadout(embeddings, normalize=False)
    embeddings[:] = np.nan
    assert readout([-1, 0]) == 0
    assert readout([1, 0]) == 1
    assert not any(isinstance(value, np.ndarray) for value in vars(readout).values())


def test_adjacent_float_threshold_and_zero_signal():
    a = np.float32(1)
    b = np.nextafter(a, np.float32(2))
    readout = PartitionTokenReadout([[a], [b]], normalize=False)
    assert readout([a]) == 0
    assert readout([b]) == 1
    assert type(readout([0])) is int


@pytest.mark.parametrize('embeddings', [[], [[]], [[float('nan')]], [[float('inf')]], [1, 2]])
def test_invalid_embedding_tables(embeddings):
    with pytest.raises(ValueError):
        PartitionTokenReadout(embeddings)


@pytest.mark.parametrize('ids', [[-1, 2], [1, 1], [1.0, 2.0], [1]])
def test_invalid_ids(ids):
    with pytest.raises(ValueError):
        PartitionTokenReadout([[0], [1]], ids)


@pytest.mark.parametrize('signal', [[1, 2], [float('nan')], [float('inf')]])
def test_invalid_signal(signal):
    readout = PartitionTokenReadout([[0], [1]])
    with pytest.raises(ValueError):
        readout(signal)


def test_save_load_tree_only_and_baseline_binding(tmp_path):
    readout = PartitionTokenReadout([[2, 0], [0, 2]], [5, 8])
    path = tmp_path / 'readout.npz'
    readout.save(path, 'baseline-sha256')
    loaded = PartitionTokenReadout.load(path, 'baseline-sha256')
    assert loaded.readout([4, 0]) == 5
    assert loaded.readout([0, 4]) == 8
    with np.load(path, allow_pickle=False) as archive:
        assert set(archive.files) == {'version', 'dimension', 'normalize',
                                      'baseline_fingerprint', 'nodes', 'thresholds'}
    with pytest.raises(ValueError, match='fingerprint mismatch'):
        PartitionTokenReadout.load(path, 'other-baseline')


def test_failed_atomic_save_preserves_previous_file(tmp_path, monkeypatch):
    import activation_field.token_readout as module
    path = tmp_path / 'readout.npz'
    path.write_bytes(b'previous artifact')
    def fail_replace(*args):
        raise OSError('synthetic replacement failure')
    monkeypatch.setattr(module.os, 'replace', fail_replace)
    with pytest.raises(OSError):
        PartitionTokenReadout([[0], [1]]).save(path, 'baseline')
    assert path.read_bytes() == b'previous artifact'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['readout.npz']


@pytest.mark.parametrize('field,value', [
    ('dimension', np.asarray(0, np.int64)),
    ('normalize', np.asarray(1, np.int64)),
    ('version', np.asarray(2, np.int64)),
    ('nodes', np.array([[-1, -1, -1, 0.5]])),
    ('nodes', np.array([[0, 0, 1, -1], [-1, -1, -1, 1]], np.int64)),
    ('nodes', np.array([[0, 1, 99, -1], [-1, -1, -1, 1]], np.int64)),
    ('nodes', np.array([[8, 1, 2, -1], [-1, -1, -1, 0], [-1, -1, -1, 1]], np.int64)),
    ('nodes', np.array([[-1, -1, -1, -2]], np.int64)),
    ('thresholds', np.array([np.nan, 0, 0], np.float64)),
    ('baseline_fingerprint', np.array('baseline', dtype=object)),
])
def test_reject_invalid_persisted_artifact(tmp_path, field, value):
    path = tmp_path / 'tree.npz'
    PartitionTokenReadout([[0], [1]], normalize=False).save(path, 'baseline')
    with np.load(path, allow_pickle=False) as archive:
        data = dict(archive)
    data[field] = value
    if field == 'nodes':
        data['thresholds'] = np.zeros(len(value), np.float64)
    np.savez(path, **data)
    with pytest.raises(ValueError):
        PartitionTokenReadout.load(path, 'baseline')


def test_prenormalized_readonly_memmap(tmp_path):
    path = tmp_path / 'embeddings.bin'
    matrix = np.memmap(path, mode='w+', dtype=np.float32, shape=(3, 2))
    matrix[:] = [[-1, 0], [0, 1], [1, 0]]
    matrix.flush()
    readonly = np.memmap(path, mode='r', dtype=np.float32, shape=(3, 2))
    readout = PartitionTokenReadout.from_embeddings(readonly, normalize=False)
    assert readout.readout([1, 0]) == 2
    np.testing.assert_array_equal(readonly, [[-1, 0], [0, 1], [1, 0]])


def test_large_finite_values_normalize_in_tiles_without_overflow():
    value = np.finfo(np.float32).max
    readout = PartitionTokenReadout([[value, value], [-value, -value]])
    assert readout.readout([value, value]) == 0
    assert readout.readout([-value, -value]) == 1


def test_deep_unbalanced_partition_has_no_recursion_limit(tmp_path):
    # A maximum-span axis separates one basis vector from all the others.
    # This yields a depth greater than Python's usual recursion limit.
    embeddings = np.eye(1050, dtype=np.float32)
    readout = PartitionTokenReadout(embeddings)
    assert len(readout._nodes) == 2099
    assert readout.readout(embeddings[-1]) == 1049
    assert readout.readout(embeddings[0]) == 0
    path = tmp_path / 'deep.npz'
    readout.save(path, 'deep-baseline')
    loaded = PartitionTokenReadout.load(path, 'deep-baseline')
    assert loaded.readout(embeddings[-1]) == 1049


@pytest.mark.parametrize('interrupt_at', [1, 2, 3, 4, 5, 6])
def test_construction_cancellation_propagates_without_save(tmp_path, interrupt_at):
    calls = 0
    def check():
        nonlocal calls
        calls += 1
        if calls == interrupt_at:
            raise InterruptedError('Construction cancelled')
    path = tmp_path / 'tree.npz'
    with pytest.raises(InterruptedError, match='Construction cancelled'):
        tree = PartitionTokenReadout.from_embeddings([[0, 1], [1, 0]], check=check)
        tree.save(path, 'baseline')
    assert calls == interrupt_at
    assert not list(tmp_path.iterdir())
