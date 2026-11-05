"""Validate a change-map shard before allocating or publishing model state."""
import hashlib
import numpy as np


def validate_entry(entry, data, shape, seen):
    idx, codes, delta = (data[k] for k in ('flat_indices','replacement_packed','signed_delta'))
    rows, words = shape
    if entry.get('tensor_name') != 'weight' or entry.get('logical_shape') != [rows, words*8]:
        raise ValueError('Change-map tensor layout mismatch')
    if idx.ndim != 1 or idx.dtype != np.uint32 or codes.dtype != np.uint32 or codes.shape != (len(idx),8):
        raise ValueError('Invalid replacement groups')
    if delta.shape != (len(idx),64) or delta.dtype.kind != 'f' or not np.isfinite(delta).all():
        raise ValueError('Invalid signed delta')
    if np.any(idx >= rows*words//8) or len(np.unique(idx)) != len(idx) or seen.intersection(map(int,idx)):
        raise ValueError('Duplicate or out-of-range change group')
    if hashlib.sha256(codes.tobytes()).hexdigest() != entry.get('checksum'):
        raise ValueError('Change-map checksum mismatch')
    seen.update(map(int,idx))
