"""Observer neutrality and discrimination, not semantic recovery tests."""
import json
from collections import Counter
from copy import deepcopy

import numpy as np
import pytest

from activation_field.boundaries import BoundaryRecord, ReverseTrajectory, accumulate
from activation_field.checkpoint import Vault
from activation_field.inverse import InverseOptions
from activation_field.inverse_runtime import reconstruct_with_adapter
from activation_field.reverse import Operation, emit_tokens, reverse_operations
from activation_field.reverse_debug import ReverseDebug
from activation_field.token_readout import PartitionTokenReadout


class Adapter:
    operations = (Operation('layer', ('state.0',), 'present'),)

    def __init__(self, signals, debug=None, collapse=False):
        self.values = [np.asarray(value, np.float32) for value in signals]
        self.debug, self.collapse = debug, collapse
        self.counts = Counter()
        self.tree = PartitionTokenReadout([[-1., 0.], [1., 0.]], normalize=False)
        self.counterparts = {'layer': self.counterpart}

    def counterpart(self, signal, *_):
        self.counts['counterpart'] += 1
        return {'state.0': np.array([1., 0.]) if self.collapse else signal}

    def begin(self, *_):return 0
    def finished(self, state):return state == len(self.values)
    def signals(self, state):return {'present': self.values[state]}
    def readout(self, flow, state):
        self.counts['readout'] += 1
        return self.tree.readout(flow['state.0'],
            trace=None if self.debug is None else self.debug.readout)
    def advance(self, state, flow):
        self.counts['advance'] += 1
        return state + 1


def run(values, debug=None, collapse=False):
    adapter = Adapter(values, debug, collapse)
    result = reconstruct_with_adapter(adapter, {}, {'layer': {}}, {}, str, debug=debug)
    return adapter, result


def metrics(report, stage):
    index = report['signal_names'].index(stage)
    return [step['signals'][index] for step in report['steps']]


@pytest.mark.parametrize('values,collapse,expected', [
    ([[1, 1], [1, 1], [1, 1]], False, [1, 1, 1]),
    ([[1, -1], [1, 1], [-1, 1]], True, [1, 1, 1]),
    ([[1, 1], [2, 1], [1, 2]], False, [1, 1, 1]),
    ([[-1, 1], [1, 1], [-1, -1]], False, [0, 1, 0]),
])
def test_observation_preserves_ids_inputs_and_call_counts(values, collapse, expected):
    source = deepcopy(values)
    plain, baseline = run(values, collapse=collapse)
    traced, actual = run(values, ReverseDebug(), collapse)
    report = actual.pop('debug')
    assert actual == baseline
    assert actual['estimated_token_ids'] == expected
    assert plain.counts == traced.counts == Counter(counterpart=3, readout=3, advance=3)
    assert values == source
    for before, after in zip(plain.values, traced.values):
        np.testing.assert_array_equal(before, after)
    json.dumps(report, allow_nan=False)


def test_trace_distinguishes_stagnation_contraction_and_partition_plateau():
    cases = [([[1, 1], [1, 1]], False),
             ([[1, -1], [1, 1]], True),
             ([[1, 1], [2, 1]], False)]
    reports = [run(values, ReverseDebug(), collapse)[1]['debug'] for values, collapse in cases]
    changes = [(metrics(r, 'layer.incoming')[1][1], metrics(r, 'readout.input')[1][1])
               for r in reports]
    assert changes[0] == (0., 0.)
    assert changes[1][0] > 0 and changes[1][1] == 0.
    assert changes[2][0] > 0 and changes[2][1] > 0
    assert all(len(r['readout_paths']) == 1 for r in reports)
    assert all(r['steps'][0]['readout']['path'] == r['steps'][1]['readout']['path'] for r in reports)


def test_readout_paths_and_threshold_margins_are_actual_comparisons():
    _, result = run([[-1, 1], [1, 1], [-1, -1]], ReverseDebug())
    report = result['debug']
    assert len(report['readout_paths']) == 2
    assert [step['readout']['path'] for step in report['steps']] == [0, 1, 0]
    for step in report['steps']:
        trace = step['readout']
        branch = report['readout_paths'][trace['path']]['branches'][0]
        assert branch[1:3] == [0, 0.]
        assert branch[3] == (step['token_id'] == 0)
        assert trace['minimum_margin'] == abs(trace['closest_split']['coordinate'])


@pytest.mark.parametrize('normalize', [True, False])
def test_traced_readout_matches_plain_across_coordinates_and_boundaries(normalize):
    rng = np.random.default_rng(27)
    tree = PartitionTokenReadout(rng.normal(size=(37, 7)), normalize=normalize)
    for vector in list(rng.normal(size=(80, 7))) + [np.zeros(7)]:
        original = vector.copy()
        events = []
        expected = tree.readout(vector)
        assert tree.readout(vector, trace=events.append) == expected
        assert len(events) == 1 and events[0]['token_id'] == expected
        np.testing.assert_array_equal(vector, original)
        details = events[0]
        for node, axis, threshold, went_left in details['branches']:
            assert went_left == (float(details['vector'][axis]) < threshold)
        assert details['minimum_margin'] == min(abs(float(details['vector'][axis])-threshold)
            for node, axis, threshold, went_left in details['branches'])


def test_scalar_only_report_and_fixed_previous_vector_storage():
    debug = ReverseDebug()
    _, result = run([[1, 1]] * 200, debug)
    report = result['debug']
    assert report['schema_version'] == 2
    assert 'write_conditioning' not in report  # Generic observers do not infer an adapter's method.
    assert len(debug._previous) == 2
    assert sum(value.size for value in debug._previous.values()) == 4
    assert len(report['readout_paths']) == 1
    assert len(report['steps']) == 200
    assert report['signal_summary']['readout.input']['unchanged'] == 199
    # No arrays, activations or source tokens are serialized. Scalar trace
    # storage grows with emissions, not with hidden-state dimension.
    json.dumps(report, allow_nan=False)
    assert not report['stores_source_tokens'] and not report['stores_signal_vectors']


def test_observer_copies_previous_vector_and_handles_zero_or_tiny_signals():
    debug = ReverseDebug()
    value = np.zeros(2, np.float32)
    for update in ([0., 0.], [1e-30, 0.], [1e-30, 1e-35]):
        value[:] = update
        debug.begin_step();debug.observe('signal', value);debug.end_step(0)
    rows = metrics(debug.report(), 'signal')
    assert rows[0] == [0., None, None, None]
    assert rows[1][1] > 0 and rows[1][2] is None
    assert rows[2][1] > 0 and rows[2][3] > 0
    json.dumps(debug.report(), allow_nan=False)


@pytest.mark.parametrize('radial', [True, False])
def test_trajectory_diagnostics_do_not_change_motion_or_termination(radial):
    record = None
    for i in range(13):
        value = [np.cos(i*.1), np.sin(i*.1)]
        record = accumulate(record, value, value, {}, 1.)
    if radial:
        arrays = dict(record.arrays, current_output=np.array([1., 0.], np.float32),
            output_mean=np.array([1., 0.], np.float32),
            backward_flow=np.array([[1.], [0.]], np.float32))
        record = BoundaryRecord(record.mass, record.samples, record.motion, arrays)
    original = deepcopy(record.arrays)
    debug = ReverseDebug()
    plain, traced = ReverseTrajectory(record), ReverseTrajectory(record, debug)
    while not plain.done:
        debug.begin_step();plain.advance();traced.advance();debug.end_step(0)
        np.testing.assert_array_equal(plain.value, traced.value)
        assert (plain.remaining, plain.done) == (traced.remaining, traced.done)
    assert debug.steps[-1]['trajectory']['stop_reason'] == 'motion_exhausted'
    if radial:
        # Existing behavior is observed, not classified as a defect or stopped.
        assert len(debug.steps) > 1
        for step in debug.steps[:-1]:
            motion = step['trajectory']
            assert motion['raw_delta_norm'] > 0
            assert motion['applied_step'] == min(motion['step'],motion['remaining_before'])
            assert motion['tangent_delta_norm'] == motion['actual_state_change'] == 0.
    for key, value in original.items():np.testing.assert_array_equal(value, record.arrays[key])


def test_zero_direction_and_single_leaf_have_defined_diagnostics():
    record = accumulate(None, [1.], [1.], {}, 1.)
    record = BoundaryRecord(2., 2, 1., record.arrays)
    debug = ReverseDebug();debug.begin_step()
    trajectory = ReverseTrajectory(record, debug);trajectory.advance()
    tree = PartitionTokenReadout([[1.]], [5])
    assert tree.readout([0.], trace=debug.readout) == 5
    debug.end_step(5)
    step = debug.steps[0]
    assert trajectory.done and step['trajectory']['stop_reason'] == 'zero_direction'
    assert step['trajectory']['applied_step'] == 0.
    assert step['readout']['minimum_margin'] is None
    assert debug.readout_paths[0]['branches'] == []
    json.dumps(debug.report(), allow_nan=False)


def test_debug_distinguishes_existing_small_step_stop_from_exhausted_motion():
    original = accumulate(None, [1.], [1.], {}, 1.)
    record = BoundaryRecord(10001., 10001, 1e-9, original.arrays)
    debug = ReverseDebug();debug.begin_step()
    plain, traced = ReverseTrajectory(record), ReverseTrajectory(record, debug)
    plain.advance();traced.advance();debug.end_step(0)
    assert plain.done and traced.done and plain.remaining == traced.remaining == 1e-9
    assert debug.steps[0]['trajectory']['stop_reason'] == 'step_below_tolerance'
    assert debug.steps[0]['trajectory']['applied_step'] == 0.


def test_debug_options_are_explicit_booleans():
    assert InverseOptions.parse().debug is False
    assert InverseOptions.parse({'debug': True}).to_dict() == {'mode': 'reverse', 'debug': True}
    for value in (None, 0, 1, 'true', []):
        with pytest.raises(ValueError, match='boolean'):InverseOptions.parse({'debug': value})


def test_debug_result_uses_existing_encrypted_storage(tmp_path):
    _, result = run([[1, 1], [2, 1]], ReverseDebug())
    vault = Vault(tmp_path)
    vault.save_json('result.enc', result)
    assert vault.load_json('result.enc') == result
    assert b'layer.incoming' not in (tmp_path / 'result.enc').read_bytes()


def test_readout_end_cancellation_and_operation_errors_stay_unchanged():
    for debug in (None, ReverseDebug()):
        adapter = Adapter([[1, 1]], debug)
        adapter.readout = lambda *_: None
        assert list(emit_tokens(adapter, {}, {'layer': {}}, {}, debug=debug)) == []
        assert adapter.counts['advance'] == 0
        def cancel():raise InterruptedError('same cancellation')
        with pytest.raises(InterruptedError, match='same cancellation'):
            list(emit_tokens(adapter, {}, {'layer': {}}, {}, check=cancel, debug=debug))
        def fail(*_):raise ValueError('same operation error')
        adapter.counterparts['layer'] = fail
        with pytest.raises(ValueError, match='same operation error'):
            list(emit_tokens(adapter, {}, {'layer': {}}, {}, debug=debug))


def test_observer_keeps_branch_merges_unchanged():
    operations = (Operation('left', ('x',), 'l'), Operation('right', ('x',), 'r'),
                  Operation('merge', ('l', 'r'), 'out'))
    callbacks = dict(left=lambda *_: {'x': np.array([1., 0.])},
        right=lambda *_: {'x': np.array([0., 1.])},
        merge=lambda *_: {'l': np.array([1., 0.]), 'r': np.array([0., 1.])})
    debug = ReverseDebug();debug.begin_step()
    traced = reverse_operations(operations, {'out': [1., 1.]}, dict.fromkeys(callbacks), {}, callbacks, debug=debug)
    plain = reverse_operations(operations, {'out': [1., 1.]}, dict.fromkeys(callbacks), {}, callbacks)
    np.testing.assert_array_equal(traced['x'], plain['x'])
