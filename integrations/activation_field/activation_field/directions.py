"""Bounded-size signed forward state, collected alongside field writes.

Records contain aggregate moments and the current boundary value, never a list
of past activations or token IDs. Projection coverage is not a full reverse
adapter for the hybrid Qwen model.
"""
from dataclasses import dataclass
import math
import numpy as np

SCHEMA_VERSION = 1
PREFIX = 'direction.'
INPUT_FIELDS = ('input_mean', 'input_flow', 'current_input')
OUTPUT_FIELDS = ('output_mean', 'output_flow', 'current_output', 'write_mean')
ARRAY_FIELDS = INPUT_FIELDS + OUTPUT_FIELDS


def normalized(value, xp=np):
    value = xp.array(value, dtype=xp.float32)
    return value / xp.maximum(xp.linalg.norm(value), 1e-12)


@dataclass(frozen=True)
class DirectionRecord:
    mass: float
    samples: int
    arrays: dict


def accumulate(record, x, y, write, mass, xp=np):
    """Update signed averages and forward movement without retaining history.

    The current boundary is needed for the next movement update. Old boundary
    values are replaced, not appended. Zero role/template mass adds no signal.
    Inputs are the writer's normalized input, output and bounded coefficient.
    """
    if not math.isfinite(mass) or mass < 0:
        raise ValueError('Direction mass must be finite and nonnegative')
    if mass == 0:
        return record
    x, y = normalized(x, xp), normalized(y, xp)
    write = xp.array(write, dtype=xp.float32)
    if x.ndim != 1 or y.ndim != 1 or write.shape != y.shape:
        raise ValueError('Invalid projection direction shape')
    if record is None:
        return DirectionRecord(mass, 1, dict(
            input_mean=x, output_mean=y,
            input_flow=xp.zeros_like(x), output_flow=xp.zeros_like(y),
            current_input=x, current_output=y, write_mean=write))
    old = record.arrays
    if x.shape != old['current_input'].shape or y.shape != old['current_output'].shape:
        raise ValueError('Direction dimensions changed')
    total = record.mass + mass
    if not math.isfinite(total):
        raise ValueError('Direction mass overflow')
    rate = mass / total
    values = dict(input_mean=x, output_mean=y,
                  input_flow=normalized(x-old['current_input'], xp),
                  output_flow=normalized(y-old['current_output'], xp),
                  write_mean=write)
    arrays = {key: old[key] + rate*(value-old[key]) for key, value in values.items()}
    arrays.update(current_input=x, current_output=y)
    return DirectionRecord(total, record.samples+1, arrays)


def metadata(records, field_version):
    return dict(schema_version=SCHEMA_VERSION, coverage='quantized_projections',
                field_version=field_version,
                projections={path: dict(mass=r.mass, samples=r.samples)
                             for path, r in records.items()})


def tensors(records):
    for path, record in records.items():
        for key in ARRAY_FIELDS:
            yield PREFIX + path + '.' + key, record.arrays[key]


def restore(meta, arrays, shapes, field_version, xp=np):
    """Validate checkpoint format, not reconstruction quality or equivalence."""
    arrays = dict(arrays)
    if meta is None:
        if arrays:
            raise ValueError('Direction tensors have no metadata')
        return {}  # Older checkpoints remain readable; no signal is invented.
    if (meta.get('schema_version') != SCHEMA_VERSION or
            meta.get('coverage') != 'quantized_projections' or
            type(meta.get('field_version')) is not int or
            meta['field_version'] != field_version):
        raise ValueError('Unsupported or mismatched direction state')
    entries = meta.get('projections')
    if not isinstance(entries, dict) or set(entries)-set(shapes):
        raise ValueError('Unknown direction projection')
    records = {}
    for path, value in entries.items():
        if not isinstance(value, dict) or set(value) != {'mass', 'samples'}:
            raise ValueError('Invalid direction record')
        mass, samples = value['mass'], value['samples']
        if (type(mass) not in (int, float) or not math.isfinite(mass) or mass <= 0 or
                type(samples) is not int or samples < 1):
            raise ValueError('Invalid direction accumulation counters')
        inputs, outputs = shapes[path]
        values = {}
        for key in ARRAY_FIELDS:
            name = PREFIX+path+'.'+key
            a = np.asarray(arrays.pop(name, None))
            size = inputs if key in INPUT_FIELDS else outputs
            if a.shape != (size,) or a.dtype != np.float32 or not np.isfinite(a).all():
                raise ValueError('Invalid direction tensor: '+name)
            values[key] = xp.array(a, dtype=xp.float32)
        records[path] = DirectionRecord(float(mass), samples, values)
    if arrays:
        raise ValueError('Unexpected direction tensor')
    return records
