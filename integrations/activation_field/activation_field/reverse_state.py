"""Transient aggregate reverse state, derived from a single saved snapshot.

These are approximate predecessor states, not recovered tokens or a history.
Nothing in this module is written back to the checkpoint.
"""
import numpy as np
from .boundaries import ReverseTrajectory
from .directions import normalized
from .attention_summary import CROSS, MASS


class ProjectionState:
    def __init__(self, record):
        self.record = record
        self.input = normalized(np.asarray(record.arrays['current_input']) +
                                np.asarray(record.arrays['input_mean']))
        self.output = normalized(np.asarray(record.arrays['current_output']) +
                                 np.asarray(record.arrays['output_mean']))
        self.write = np.array(record.arrays['write_mean'], dtype=np.float32)
        self.remaining = max(0, record.samples-1)
        self.moving = any(np.linalg.norm(np.asarray(record.arrays[key])) > 1e-12
                          for key in ('input_flow', 'output_flow'))

    @property
    def done(self):return not self.moving or self.remaining == 0

    def advance(self):
        if self.done:return
        distance = 1.
        self.remaining -= 1
        # Recorded flows point forwards. Keep their measured magnitude rather
        # than amplifying weak/cancelled mean flows into unit displacements.
        self.input = normalized(self.input - distance*np.asarray(self.record.arrays['input_flow']))
        self.output = normalized(self.output - distance*np.asarray(self.record.arrays['output_flow']))


def remove_paired_cell(summary, key_features, value):
    """Remove one inferred historical cell, with a feasible nonnegative mass.

    A summary cannot identify the last cell. Project its inferred contribution
    onto the available feature mass; use that SAME feature vector in the cross
    subtraction. No missing rotating-cache cells are invented or replenished.
    """
    phi = np.asarray(key_features, np.float32).squeeze(-2)
    value = np.asarray(value, np.float32).squeeze(-2)
    mass = np.asarray(summary[MASS], np.float32)
    removed = np.minimum(np.maximum(phi, 0), mass)
    remaining = np.maximum(mass - removed, 0)
    cross = np.asarray(summary[CROSS], np.float32) - removed[..., :, None]*value[..., None, :]
    cross = np.where(remaining[..., :, None] > 0, cross, 0)
    return {MASS: remaining, CROSS: cross}


def reverse_conv_mean(current, newest, kernel_slots, prior=None):
    """One least-squares predecessor correction for the mean-window surrogate.

    F(c) = ((slots-1)c + newest)/slots.  c <- c + F'(c)^T(c-F(c))
    with an equal ridge prior is one regularized Landweber step, not division by a nearly
    singular coefficient or an attempt to recreate an unknown token window.
    """
    if kernel_slots < 1:
        return np.array(current, dtype=np.float32, copy=True)
    fraction = (kernel_slots-1)/kernel_slots
    predicted = fraction*current + newest/kernel_slots
    if prior is None:prior = current
    return regularized_predecessor(current, fraction*(current-predicted), prior)


def regularized_predecessor(current, residual_pullback, prior):
    """One ridge-regularized predecessor step for a contractive transition.

    Minimize .5||F(z)-current||² + .5||z-prior||², starting at current.
    The native normalized delta recurrence and mean-window transition have
    operator norm <= 1, so a half step is the analytic 1/(1+1) smoothness bound.
    The fixed final aggregate prior regularizes unavailable historical state.
    This is NOT an inverse, convergence solve, norm clipping or token limit.
    """
    return np.asarray((current + prior + residual_pullback)*.5, np.float32)


class LayerState:
    def __init__(self, record):
        self.trajectory = ReverseTrajectory(record)
        self.initial_output = self.trajectory.value.copy()
        self.input = np.array(record.arrays['input_mean'], dtype=np.float32)
        self.context = {k[8:]:np.array(v, dtype=np.float32, copy=True)
                        for k,v in record.arrays.items() if k.startswith('context.')}
        self.position = record.context_metadata.get('query_position', 0)
        self.past_count = record.context_metadata.get('past_count')
        self.prefix_count = record.context_metadata.get('prefix_count', 0)
        self.tail_start = record.context_metadata.get('tail_start', 0)
        self.pending = None

    def has_predecessor_cell(self):
        position = self.position-1
        return bool(self.past_count and position >= 0 and
                    (position < self.prefix_count or position >= self.tail_start))

    def advance(self, pullback):
        before = self.trajectory.value.copy()
        self.trajectory.advance()
        distance = float(np.linalg.norm(self.trajectory.value-before))
        if distance:
            # The local reverse signal steers the input direction using this
            # layer's movement distance. This is a surrogate, not a Jacobian
            # inverse of the output displacement. Preserve aggregate magnitude.
            scale = float(np.linalg.norm(self.input))
            self.input = scale*normalized(self.input + scale*distance*normalized(pullback))
        return distance


class ModelTrajectory:
    """Stop only when all recorded boundary movement is exhausted/stationary."""
    def __init__(self, records, projections, debug=None):
        self.layers = {name:LayerState(record) for name,record in records.items()
                       if name != 'final_norm'}
        self.terminal = ReverseTrajectory(records['final_norm'], debug=debug)
        self.projections = {path:ProjectionState(record) for path,record in projections.items()}
        self.pullbacks = {}

    @property
    def value(self):return self.terminal.value

    @property
    def done(self):
        return (self.terminal.done and all(state.trajectory.done for state in self.layers.values())
                and all(state.done for state in self.projections.values()))
