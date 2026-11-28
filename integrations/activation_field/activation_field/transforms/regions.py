"""AFWT v1 reference arithmetic: decoded float32 parameters, binary64 sums."""
from dataclasses import dataclass, asdict
import math
import numpy as np
from .common import TransformError


@dataclass(frozen=True)
class Region:
    id: int
    tensor_id: int
    c0: float
    c1: float
    log_h0: float
    log_h1: float
    angle: float
    a: float
    b: float = 0.
    mu: float = 0.
    hv: float = 1.
    exponent: int = 1
    window: int = 0
    kernel: int = 0
    reserved: int = 0

    def validate(self):
        for key in ('id', 'tensor_id'):
            value = getattr(self, key)
            if type(value) is not int or not 0 <= value < 2**32:
                raise TransformError('invalid_artifact', 'Invalid region identity')
        if not all(math.isfinite(float(getattr(self, k))) for k in ('c0', 'c1', 'log_h0', 'log_h1', 'angle', 'a', 'b', 'mu', 'hv')):
            raise TransformError('invalid_artifact', 'Nonfinite region')
        try:
            radii = (math.exp(self.log_h0), math.exp(self.log_h1))
        except OverflowError as exc:
            raise TransformError('invalid_artifact', 'Unrepresentable radius') from exc
        if not all(math.isfinite(h) and h > 0 for h in radii):
            raise TransformError('invalid_artifact', 'Unrepresentable radius')
        if self.exponent not in (1, 2, 4) or self.window not in (0, 1) or self.kernel != 0 or self.reserved != 0:
            raise TransformError('invalid_artifact', 'Unknown region encoding')
        if (self.window and self.hv <= 0) or (not self.window and self.hv != 1):
            raise TransformError('invalid_artifact', 'Invalid value radius')
        return self

    def basis(self, x0, x1, u):
        self.validate()
        dx, dy = x0 - self.c0, x1 - self.c1
        co, si = math.cos(self.angle), math.sin(self.angle)
        with np.errstate(over='ignore', divide='ignore'):
            d = np.hypot((co * dx + si * dy) / math.exp(self.log_h0),
                         (-si * dx + co * dy) / math.exp(self.log_h1))
        phi = np.maximum(0., 1. - d) ** self.exponent
        if self.window:
            with np.errstate(over='ignore', divide='ignore'):
                phi = phi * np.maximum(0., 1. - np.abs(u - self.mu) / self.hv) ** self.exponent
        return phi, phi * (u - self.mu)

    def support_bounds(self):
        co, si = math.cos(self.angle), math.sin(self.angle)
        h0, h1 = math.exp(self.log_h0), math.exp(self.log_h1)
        rx = math.hypot(h0 * co, h1 * si)
        ry = math.hypot(h0 * si, h1 * co)
        return self.c0-rx, self.c0+rx, self.c1-ry, self.c1+ry

    def to_dict(self):
        return asdict(self)


def evaluate_delta(regions, descriptor, rows, columns, values):
    values = np.asarray(values, dtype=np.float64)
    rows, columns, values = np.broadcast_arrays(rows, columns, values)
    nrows, ncols = descriptor['logical_shape']
    if np.any(rows < 0) or np.any(rows >= nrows) or np.any(columns < 0) or np.any(columns >= ncols):
        raise ValueError('Coordinate outside logical tensor')
    s = descriptor['s_m']
    x0, x1, u = (rows + .5)/nrows, (columns + .5)/ncols, values/s
    delta = np.zeros(values.shape, np.float64)
    # Always evaluate against the original value. Order is part of the format.
    for r in sorted(regions, key=lambda r: r.id):
        if r.tensor_id == descriptor['id']:
            phi, affine = r.basis(x0, x1, u)
            delta += s * (phi*r.a + affine*r.b)
    if not np.isfinite(delta).all():
        raise TransformError('nonfinite_result', 'Transform produced a nonfinite value')
    return delta


def evaluate_transform(transform, tensor_id, coordinate, baseline_value):
    manifest, regions = transform
    descriptor = next((d for d in manifest['tensor_map'] if d['id'] == tensor_id or d['path'] == tensor_id), None)
    if descriptor is None:
        raise TransformError('unsupported_layout', 'Unknown tensor')
    row, column = coordinate
    return float(baseline_value + evaluate_delta(regions, descriptor, row, column, baseline_value))
