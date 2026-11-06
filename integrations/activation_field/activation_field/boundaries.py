"""Aggregate operation state and a signed backward movement field.

Only running moments and the current boundary are kept. Attention history is
reduced to aggregate context by the model adapter before it reaches this module.
"""
from dataclasses import dataclass
import math
import numpy as np
from .directions import normalized

PREFIX = 'boundary.'


@dataclass(frozen=True)
class BoundaryRecord:
    mass: float
    samples: int
    motion: float
    arrays: dict


def features(value, xp=np):
    """Fixed coordinate features; their count depends on state dimension."""
    size = value.size
    count = max(1, int(math.sqrt(size)))
    indices = np.linspace(0, size-1, count).astype(np.int32)
    return xp.tanh(value[xp.array(indices)] * math.sqrt(size)) / math.sqrt(count)


def accumulate(record, x, y, context, mass, xp=np):
    if not math.isfinite(mass) or mass < 0:
        raise ValueError('Invalid boundary mass')
    if not mass:
        return record
    x, y = xp.array(x, dtype=xp.float32).reshape(-1), xp.array(y, dtype=xp.float32).reshape(-1)
    unit = normalized(y, xp)
    phi = features(unit, xp)
    context = {'context.'+key:xp.array(value, dtype=xp.float32) for key,value in context.items()}
    if record is None:
        arrays = dict(input_mean=x, output_mean=y, current_output=unit,
                      backward_flow=xp.zeros((y.size, phi.size), dtype=xp.float32))
        arrays.update(context)
        return BoundaryRecord(mass, 1, 0., arrays)
    if x.shape != record.arrays['input_mean'].shape or y.shape != record.arrays['output_mean'].shape:
        raise ValueError('Boundary dimensions changed')
    total = record.mass+mass
    rate = mass/total
    backward = record.arrays['current_output']-unit
    flow = backward[:, None]*phi[None, :]
    arrays = dict(input_mean=record.arrays['input_mean']+rate*(x-record.arrays['input_mean']),
                  output_mean=record.arrays['output_mean']+rate*(y-record.arrays['output_mean']),
                  current_output=unit,
                  backward_flow=record.arrays['backward_flow']+rate*(flow-record.arrays['backward_flow']))
    arrays.update(context)
    motion = record.motion+float(xp.linalg.norm(backward).item())
    if not math.isfinite(total) or not math.isfinite(motion):
        raise ValueError('Boundary accumulation overflow')
    return BoundaryRecord(total, record.samples+1, motion, arrays)


def metadata(records, version):
    return dict(schema_version=1, field_version=version,
                operations={name:dict(mass=r.mass,samples=r.samples,motion=r.motion,
                                     shapes={key:list(a.shape) for key,a in r.arrays.items()})
                            for name,r in records.items()})


def tensors(records):
    for name, record in records.items():
        for key, value in record.arrays.items():
            yield PREFIX+name+'.'+key, value


def restore(meta, tensors, version, dimensions, xp=np):
    data = dict(tensors)
    if meta is None:
        if data:raise ValueError('Boundary tensors without metadata')
        return {}
    if meta.get('schema_version') != 1 or type(meta.get('field_version')) is not int or meta['field_version'] != version:
        raise ValueError('Boundary schema or field version mismatch')
    operations = meta.get('operations')
    if not isinstance(operations,dict) or set(operations)-set(dimensions):
        raise ValueError('Unknown boundary operation')
    records = {}
    for name, info in operations.items():
        if not isinstance(info,dict) or set(info) != {'mass','samples','motion','shapes'}:
            raise ValueError('Invalid boundary metadata')
        mass, samples, motion = info['mass'],info['samples'],info['motion']
        if (type(mass) not in (int,float) or not math.isfinite(mass) or mass <= 0 or
                type(samples) is not int or samples < 1 or type(motion) not in (int,float) or
                not math.isfinite(motion) or motion < 0):
            raise ValueError('Invalid boundary counters')
        inputs,outputs,contexts = dimensions[name]
        expected = dict(input_mean=[inputs],output_mean=[outputs],current_output=[outputs],
                        backward_flow=[outputs,max(1,int(math.sqrt(outputs)))])
        expected.update({'context.'+key:list(shape) for key,shape in contexts.items()})
        if info['shapes'] != expected:raise ValueError('Boundary tensor shapes disagree with model')
        arrays = {}
        for key,shape in expected.items():
            value = np.asarray(data.pop(PREFIX+name+'.'+key,None))
            if value.dtype != np.float32 or list(value.shape) != shape or not np.isfinite(value).all():
                raise ValueError('Invalid boundary tensor')
            arrays[key] = xp.array(value, dtype=xp.float32)
        records[name] = BoundaryRecord(float(mass),samples,float(motion),arrays)
    if data:raise ValueError('Unknown boundary tensor')
    return records


class ReverseTrajectory:
    """Move through accumulated directed state until recorded motion is spent."""
    def __init__(self, record, debug=None):
        self.record = record
        self.debug = debug
        self.value = normalized(record.arrays['current_output']+normalized(record.arrays['output_mean']))
        self.remaining = record.motion
        self.step = record.motion/max(1,record.samples-1)
        self.done = False

    def advance(self):
        if self.remaining <= 1e-12 or self.step <= 1e-12:
            if self.debug is not None:
                self.debug.movement(self.value,None,self.value,self.remaining,
                                    self.remaining,self.step,
                                    'motion_exhausted' if self.remaining <= 1e-12 else 'step_below_tolerance')
            self.done = True
            return
        delta = self.record.arrays['backward_flow'] @ features(self.value)
        if float(np.linalg.norm(delta)) <= 1e-12:
            if self.debug is not None:
                self.debug.movement(self.value,delta,self.value,self.remaining,
                                    self.remaining,self.step,'zero_direction')
            self.done = True
            return
        previous = self.value
        remaining_before = self.remaining
        self.value = normalized(self.value+min(self.step,self.remaining)*normalized(delta))
        self.remaining = max(0.,self.remaining-self.step)
        if self.debug is not None:
            self.debug.movement(previous,delta,self.value,remaining_before,
                                self.remaining,self.step)
