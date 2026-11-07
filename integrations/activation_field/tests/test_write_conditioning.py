"""Magnitude fidelity, not a requirement that successive tokens differ."""
import numpy as np
import pytest

from activation_field.directions import normalized
from activation_field.reverse import combine_write_moments


@pytest.mark.parametrize('dimension', [2, 17, 4096])
@pytest.mark.parametrize('scale', [0., 1e-8, 1e-4, .02])
def test_recorded_strength_and_sign_are_not_promoted_to_unit_magnitude(dimension, scale):
    moment = np.linspace(-1., 1., dimension, dtype=np.float32)
    original = moment.copy()
    result = combine_write_moments(np.zeros(dimension, np.float32), [scale*moment])
    np.testing.assert_array_equal(result, np.float32(scale)*moment)
    np.testing.assert_array_equal(moment, original)


@pytest.mark.parametrize('dimension', [3, 64, 4096])
def test_absent_zero_and_cancelled_moments_keep_signal(dimension):
    signal = np.full(dimension, .5, np.float32)
    moment = np.full(dimension, .125, np.float32)
    for moments in ([], [np.zeros(dimension)], [moment, -moment]):
        combined = combine_write_moments(signal, moments)
        np.testing.assert_array_equal(combined, signal)
        assert not np.shares_memory(combined, signal)


@pytest.mark.parametrize('layers', [1, 8, 32, 64])
@pytest.mark.parametrize('dimension', [2, 64])
def test_weak_conditioning_preserves_distinguishable_signals_across_layers(layers, dimension):
    left = np.zeros(dimension, np.float32);left[0] = 1.
    right = np.zeros(dimension, np.float32);right[1] = 1.
    moments = [np.full(dimension, 1e-5, np.float32), np.full(dimension, -2e-6, np.float32)]
    for _ in range(layers):
        left = normalized(combine_write_moments(left, moments))
        right = normalized(combine_write_moments(right, moments))
    # A direction-space regression, not a token-diversity gate or output filter.
    assert np.linalg.norm(left-right) > 1.


def test_nonzero_moments_still_condition_the_signal_without_mutating_state():
    signal = np.array([1., 0.], np.float32)
    moments = [np.array([0., .25], np.float32), np.array([-.125, 0.], np.float32)]
    originals = [value.copy() for value in moments]
    np.testing.assert_array_equal(combine_write_moments(signal, iter(moments)), [.875, .25])
    np.testing.assert_array_equal(signal, [1., 0.])
    for actual, original in zip(moments, originals):np.testing.assert_array_equal(actual, original)
    with pytest.raises(ValueError, match='dimensions'):
        combine_write_moments(signal, [np.ones(3)])
