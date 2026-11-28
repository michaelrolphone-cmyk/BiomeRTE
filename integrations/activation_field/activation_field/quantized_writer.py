"""Tiled fixed-affine stochastic writer, with a NumPy arithmetic oracle.

The reference is for mechanics tests, not a substitute model. Packed uint32
words contain eight consecutive four-bit codes, least-significant nibble first.
Signed nonzero MLX affine scales are supported (zero-scale groups are frozen).
"""
import numpy as np


def unpack(packed):
    return ((np.asarray(packed, np.uint32)[..., None] >> (4*np.arange(8, dtype=np.uint32))) & 15).reshape(*packed.shape[:-1], packed.shape[-1]*8)


def pack(codes):
    codes = np.asarray(codes, np.uint32)
    return np.bitwise_or.reduce(codes.reshape(*codes.shape[:-1], -1, 8) << (4*np.arange(8, dtype=np.uint32)), axis=-1)


def uniform_counters(indices, seed):
    # Identical integer counter hash in the Metal kernel; independent of tiling.
    with np.errstate(over="ignore"):
        v = np.asarray(indices, np.uint32) ^ np.uint32(seed)
        v = (v ^ (v >> 16)) * np.uint32(0x7feb352d)
        v = (v ^ (v >> 15)) * np.uint32(0x846ca68b)
        v ^= v >> 16
    return (v >> 8).astype(np.float32) / np.float32(16777216)


def write_coefficients(z, gate, *, eta, role_scale, step_fraction_max, xp=np):
    """The signed bounded row coefficient, shared with direction capture."""
    coefficient = eta*role_scale*z*gate
    return coefficient*xp.minimum(1, step_fraction_max/xp.maximum(xp.abs(coefficient), 1e-12))


def reference_write(packed, scales, offsets, x, z, gate, *, eta, role_scale,
                    step_fraction_max, seed, baseline_pull=0., anchor=None,
                    delta_n=1, tile_rows=16):
    """Return new packed bytes and measured proposal/realization diagnostics."""
    p = np.asarray(packed, np.uint32)
    rows, words = p.shape
    dim = words*8
    group = dim // scales.shape[1]
    if group != 64:
        raise ValueError("Expected group size 64")
    # k=0 neither evaluates nor reads anchor, including a lazy callable anchor.
    base = (anchor() if callable(anchor) else anchor) if baseline_pull > 0 else None
    if baseline_pull > 0 and base is None:
        raise ValueError("Positive baseline pull requires an anchor")
    out = p.copy()
    stats = dict(changed_codes=0, candidate_codes=0, saturated_codes=0,
                 proposal_squared_norm=0., realized_squared_norm=0., quantization_squared_error=0.)
    for start in range(0, rows, tile_rows):
        end = min(rows,start+tile_rows)
        codes = unpack(p[start:end])
        s = np.repeat(np.asarray(scales[start:end],np.float32),group,axis=-1)
        b = np.repeat(np.asarray(offsets[start:end],np.float32),group,axis=-1)
        w = codes.astype(np.float32)*s+b
        norm = np.maximum(np.linalg.norm(w,axis=-1),1e-6)
        coefficient = write_coefficients(np.asarray(z[start:end]), np.asarray(gate[start:end]),
                                         eta=eta, role_scale=role_scale, step_fraction_max=step_fraction_max)
        proposal = w + (norm*coefficient)[:,None]*x
        if baseline_pull > 0:
            w0 = unpack(base[start:end]).astype(np.float32)*s+b
            proposal = w0 + np.float32(np.exp(-baseline_pull*delta_n))*(proposal-w0)
        candidate = (proposal != w) & (s != 0)
        r = np.where(s != 0,(proposal-b)/np.where(s != 0,s,1),codes)
        stats['saturated_codes'] += int(np.count_nonzero(candidate & ((r<0)|(r>15))))
        r=np.clip(r,0,15);lo=np.floor(r)
        u=uniform_counters(np.arange(start*dim,end*dim,dtype=np.uint32).reshape(end-start,dim),seed)
        q=np.where(candidate,lo+(u < r-lo),codes).astype(np.uint32)
        realized=q.astype(np.float32)*s+b
        out[start:end]=pack(q)
        stats['candidate_codes']+=int(np.count_nonzero(candidate))
        stats['changed_codes']+=int(np.count_nonzero(q != codes))
        stats['proposal_squared_norm']+=float(np.sum((proposal-w)**2,dtype=np.float64))
        stats['realized_squared_norm']+=float(np.sum((realized-w)**2,dtype=np.float64))
        stats['quantization_squared_error']+=float(np.sum((realized-proposal)**2,dtype=np.float64))
    return out,stats

