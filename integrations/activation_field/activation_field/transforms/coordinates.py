"""Logical coordinates, stable splits, and fixed-baseline quantization."""
import hashlib
import numpy as np
from .common import TransformError


def unpack(words):
    words = np.asarray(words, np.uint32)
    return ((words[..., None] >> (4*np.arange(8, dtype=np.uint32))) & 15).reshape(words.shape[0], -1).astype(np.uint8)


def pack(codes):
    codes = np.asarray(codes, np.uint32)
    return np.bitwise_or.reduce(codes.reshape(codes.shape[0], -1, 8) << (4*np.arange(8, dtype=np.uint32)), axis=-1)


def split_ids(path, shape, start, count, seed, fractions):
    """SplitMix64 of canonical tensor name, seed and logical flat index."""
    salt = int.from_bytes(hashlib.sha256((str(seed)+':'+path).encode()).digest()[:8], 'little')
    z = np.arange(start*shape[1], (start+count)*shape[1], dtype=np.uint64) ^ np.uint64(salt)
    with np.errstate(over='ignore'):
        z += np.uint64(0x9e3779b97f4a7c15)
        z = (z ^ (z >> 30))*np.uint64(0xbf58476d1ce4e5b9)
        z = (z ^ (z >> 27))*np.uint64(0x94d049bb133111eb)
        z ^= z >> 31
    uniform = (z >> 11).astype(np.float64)/2**53
    return np.searchsorted(np.cumsum(fractions)[:-1], uniform, side='right').reshape(count, shape[1])


def quantize(tile, delta):
    base = tile['base_codes']
    scale = np.broadcast_to(tile['scale'], base.shape)
    offset = np.broadcast_to(tile['offset'], base.shape)
    movable = (scale != 0) & (delta != 0)
    proposed = tile['base'] + delta
    if not np.isfinite(proposed).all():
        raise TransformError('nonfinite_result', 'Nonfinite materialized weight')
    q = base.copy()
    raw = np.zeros(base.shape, np.float64)
    np.divide(proposed-offset, scale, out=raw, where=movable)
    q[movable] = np.clip(np.rint(raw[movable]), 0, 15).astype(np.uint8)
    return q, dict(clipped=int(np.count_nonzero(movable & ((raw < 0) | (raw > 15)))),
                   erased=int(np.count_nonzero(movable & (q == base))),
                   zero_scale=int(np.count_nonzero(scale == 0)))


def tile_coordinates(descriptor, tile):
    return np.arange(tile['start'], tile['start']+len(tile['base']))[:, None], np.arange(descriptor['logical_shape'][1])[None, :]
