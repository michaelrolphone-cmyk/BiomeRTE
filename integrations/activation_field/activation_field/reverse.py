"""Operation-wise approximate reverse traversal, with no token search/replay.

A model adapter supplies its *complete* operation graph, recurrence traversal,
and direct token readout. These generic mechanics are not a Qwen decoder.
"""
from dataclasses import dataclass
import numpy as np
from .directions import normalized
from .quantized_writer import unpack


class ReverseUnavailable(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Operation:
    name: str
    inputs: tuple
    output: str


def combine_write_moments(signal, moments):
    """Condition a signal using the recorded signed write magnitudes.

    A write mean is a bounded coefficient, not a unit direction. Normalizing
    each mean separately amplifies weak or cancelled writes to full signal
    strength repeatedly across layers. Keep its measured magnitude instead;
    normalization of the combined signal belongs to the layer counterpart.
    """
    combined = np.array(signal, dtype=np.float32, copy=True)
    for moment in moments:
        value = np.asarray(moment, dtype=np.float32)
        if value.shape != combined.shape:
            raise ValueError('Write moment and reverse signal dimensions disagree')
        combined += value
    return combined


def quantized_backprojection(output, packed, scales, offsets, tile_rows=16):
    """Tiled transpose propagation through current affine4 weights.

    This is one approximate projection counterpart. It is not an inverse of a
    Qwen layer or a decoder, and performs no target-fitting optimization.
    """
    packed = np.asarray(packed)
    scales, offsets = np.asarray(scales), np.asarray(offsets)
    output = np.asarray(output, np.float32)
    if (packed.dtype != np.uint32 or packed.ndim != 2 or
            output.shape != (packed.shape[0],) or
            scales.shape != (packed.shape[0], packed.shape[1]//8) or
            offsets.shape != scales.shape or type(tile_rows) is not int or tile_rows < 1):
        raise ValueError('Invalid affine4 projection counterpart inputs')
    result = np.zeros(packed.shape[1]*8, np.float32)
    for start in range(0, packed.shape[0], tile_rows):
        stop = min(start+tile_rows, packed.shape[0])
        weight = unpack(packed[start:stop]).astype(np.float32)
        weight *= np.repeat(scales[start:stop], 64, axis=1)
        weight += np.repeat(offsets[start:stop], 64, axis=1)
        result += output[start:stop] @ weight
    return normalized(result)


def reverse_operations(operations, signals, directions, weights, counterparts, check=lambda: None,
                       debug=None):
    """Traverse a supplied forward DAG backwards and normalize merged flow.

    Counterparts receive the combined output, aggregate direction record and
    current weights. Missing operations are explicit errors, never skipped.
    """
    operations = tuple(operations)
    if len({op.name for op in operations}) != len(operations):
        raise ValueError('Duplicate reverse operation')
    missing = [op.name for op in operations if op.name not in counterparts]
    if missing:
        raise ReverseUnavailable('reverse_counterparts_missing', ', '.join(missing))
    flow = {key: normalized(value) for key, value in signals.items()}
    for op in reversed(operations):
        check()
        if op.output not in flow or op.name not in directions:
            raise ReverseUnavailable('reverse_direction_missing', op.name)
        incoming = normalized(flow.pop(op.output))
        if debug is not None:debug.observe(op.name+'.incoming', incoming)
        inputs = counterparts[op.name](incoming, directions[op.name], weights)
        if set(inputs) != set(op.inputs):
            raise ValueError('Counterpart returned wrong inputs: '+op.name)
        for key, value in inputs.items():
            value = np.asarray(value, np.float32)
            if not np.isfinite(value).all():
                raise FloatingPointError('Nonfinite reverse flow: '+op.name)
            if key in flow:
                if flow[key].shape != value.shape:
                    raise ValueError('Reverse branch shapes disagree: '+op.name)
                value = flow[key]+value
            flow[key] = value
    return {key: normalized(value) for key, value in flow.items()}


def emit_tokens(adapter, present_state, directions, weights, check=lambda: None, debug=None):
    """Emit until the adapter's trajectory/readout ends, without a length cap.

    The readout returns a single token ID (or None for its end signal), not a
    candidate list. advance evolves the reverse trajectory, never forwards a
    proposed token through the original model. No quality gate follows output.
    """
    trajectory = adapter.begin(present_state, directions, weights)
    while not adapter.finished(trajectory):
        check()
        if debug is not None:debug.begin_step()
        flow = reverse_operations(adapter.operations, adapter.signals(trajectory),
                                  directions, weights, adapter.counterparts, check, debug)
        token = adapter.readout(flow, trajectory)
        if token is None:
            if debug is not None:debug.end_step(None)
            return
        if type(token) is not int or token < 0:
            raise ValueError('Reverse readout must return one nonnegative token ID')
        # This is the state for the next emission, not a post-output analysis.
        trajectory = adapter.advance(trajectory, flow)
        if debug is not None:debug.end_step(token)
        yield token
