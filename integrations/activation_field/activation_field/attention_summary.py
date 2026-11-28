"""Fixed-size paired attention state; no retained token, key or value sequence.

The positive ELU kernel is a linear-attention surrogate, not Qwen's softmax.
Native caches are read only to maintain the surviving-window moments. A small
private tracker accelerates updates; it is disposable and never checkpointed.
"""
import numpy as np

FEATURE_MAP = 'elu_plus_one_v1'
CROSS = 'attention_cross'
MASS = 'attention_mass'


def positive_features(value, xp=np):
    value = value.astype(xp.float32) * value.shape[-1] ** -.25
    # ELU(x)+1 without an eager exp(large positive x). A positive floor also
    # prevents an all-negative query from producing a zero denominator.
    return xp.maximum(xp.maximum(value, 0) + xp.exp(xp.minimum(value, 0)), 1e-6)


def paired_moments(keys, values, xp=np):
    phi = positive_features(keys, xp)
    return {CROSS: xp.swapaxes(phi, -1, -2) @ values.astype(xp.float32),
            MASS: xp.sum(phi, axis=-2)}


def paired_attention(queries, keys, values, summary, xp=np):
    """Grouped-head response, including differentiable current KV exactly once."""
    batch, heads, length, dimension = queries.shape
    kv_heads = keys.shape[1]
    if heads % kv_heads or length != 1 or keys.shape[2] != 1:
        raise ValueError('Paired reverse attention requires one query/current cell')
    q = positive_features(queries, xp).reshape(batch, kv_heads, heads // kv_heads, length, dimension)
    current = paired_moments(keys, values, xp)
    cross = summary[CROSS] + current[CROSS]
    mass = summary[MASS] + current[MASS]
    numerator = q @ xp.expand_dims(cross, 2)
    denominator = xp.sum(q * mass[:, :, None, None, :], axis=-1, keepdims=True)
    return (numerator / xp.maximum(denominator, 1e-12)).reshape(batch, heads, length, values.shape[-1]).astype(queries.dtype)


def validate_descriptor(info):
    expected = {'kind', 'feature_map', 'query_position', 'cache_kind',
                'cache_capacity', 'cache_keep', 'past_count', 'prefix_count',
                'tail_start', 'tail_end'}
    if not isinstance(info, dict) or set(info) != expected:
        raise ValueError('Invalid attention boundary descriptor')
    if info['kind'] != 'paired_kernel' or info['feature_map'] != FEATURE_MAP:
        raise ValueError('Unsupported attention summary')
    for key in expected - {'kind', 'feature_map', 'cache_kind', 'cache_capacity'}:
        if type(info[key]) is not int or info[key] < 0:
            raise ValueError('Invalid attention boundary counter')
    position, count, prefix = info['query_position'], info['past_count'], info['prefix_count']
    capacity, keep = info['cache_capacity'], info['cache_keep']
    if info['cache_kind'] == 'rotating':
        if type(capacity) is not int or capacity < 1 or keep >= capacity:
            raise ValueError('Invalid rotating attention boundary')
        expected_count = min(position, capacity - 1)
        expected_prefix = min(position, keep)
    elif info['cache_kind'] == 'growing':
        if capacity is not None or keep != 0:
            raise ValueError('Invalid growing attention boundary')
        expected_count, expected_prefix = position, 0
    else:
        raise ValueError('Unsupported attention cache')
    if (count != expected_count or prefix != expected_prefix or prefix > count or
            info['tail_end'] != position or info['tail_start'] != position - (count - prefix)):
        raise ValueError('Inconsistent attention boundary positions')


def legacy_shapes(contexts):
    """Model-compatible v1 shapes; never invent missing paired state."""
    if CROSS not in contexts:
        return contexts
    batch, heads, dimension, value_dim = contexts[CROSS]
    return dict(key_mean=(batch, heads, 1, dimension),
                value_mean=(batch, heads, 1, value_dim))


def validate_summary(info, arrays):
    validate_descriptor(info)
    mass, cross = arrays['context.' + MASS], arrays['context.' + CROSS]
    if np.any(mass < 0) or (info['past_count'] and not np.all(mass > 0)):
        raise ValueError('Invalid attention feature mass')
    if info['past_count'] == 0 and (np.any(mass) or np.any(cross)):
        raise ValueError('Nonempty attention summary at empty boundary')


def prepare_context(cache, heads, dimension, xp=np):
    """Capture pre-query survivors, matching native one-token cache updates.

    Called before the native layer mutates the cache. Incremental subtraction
    removes the cell about to be evicted. Rebuild once per ring wrap and after
    an unobserved interval/restore to bound floating-point subtraction drift.
    """
    position = int(cache.offset)
    rotating = hasattr(cache, 'max_size')
    capacity = int(cache.max_size) if rotating else None
    keep = int(cache.keep) if rotating else 0
    if position < 0 or (rotating and not 0 <= keep < capacity):
        raise ValueError('Invalid attention cache boundary')
    count = min(position, capacity - 1) if rotating else position
    prefix = min(position, keep) if rotating else 0
    info = dict(kind='paired_kernel', feature_map=FEATURE_MAP,
                query_position=position, cache_kind='rotating' if rotating else 'growing',
                cache_capacity=capacity, cache_keep=keep, past_count=count,
                prefix_count=prefix, tail_start=position-(count-prefix), tail_end=position)
    validate_descriptor(info)
    if position == 0:
        summary = {CROSS: xp.zeros((1, heads, dimension, dimension), dtype=xp.float32),
                   MASS: xp.zeros((1, heads, dimension), dtype=xp.float32)}
        return summary, info
    keys, values = cache.keys_and_values()
    tracker = getattr(cache, '_boundary_attention_summary', None)
    # Native one-token updates first trim oversized multi-token prefill caches.
    oversized = rotating and keys.shape[2] > capacity
    wrap = rotating and int(cache._idx) == capacity
    identity = (position, id(cache.keys), id(cache.values), capacity, keep,
                int(cache._idx) if rotating else position)
    if tracker is not None and tracker[0] == identity and not oversized and not wrap:
        summary = dict(tracker[1])
    else:
        if oversized:
            trim = keys.shape[2] - capacity
            keys = xp.concatenate((keys[:, :, :keep, :], keys[:, :, keep+trim:, :]), axis=2)
            values = xp.concatenate((values[:, :, :keep, :], values[:, :, keep+trim:, :]), axis=2)
        summary = paired_moments(keys, values, xp)
    if rotating and position >= capacity:
        index = keep if oversized or wrap else int(cache._idx)
        removed = paired_moments(keys[:, :, index:index+1, :], values[:, :, index:index+1, :], xp)
        summary = {key: summary[key] - removed[key] for key in summary}
        # Roundoff only: actual positive feature sums cannot be negative.
        summary[MASS] = xp.maximum(summary[MASS], 0)
    if count == 0:
        summary = {key: xp.zeros_like(value) for key, value in summary.items()}
    return summary, info


def finish_context(cache, summary, info, xp=np):
    """Update only the disposable aggregate tracker after the native call."""
    if int(cache.offset) != info['query_position'] + 1:
        raise ValueError('Boundary capture requires a one-token cache update')
    index = int(cache._idx) - 1 if info['cache_kind'] == 'rotating' else int(cache.offset) - 1
    current = paired_moments(cache.keys[:, :, index:index+1, :],
                             cache.values[:, :, index:index+1, :], xp)
    post = {key: summary[key] + current[key] for key in summary}
    # Evaluate now so lazy graphs cannot retain a per-token chain or old caches.
    if xp is not np:
        xp.eval(*post.values())
    identity = (int(cache.offset), id(cache.keys), id(cache.values),
                info['cache_capacity'], info['cache_keep'],
                int(cache._idx) if info['cache_kind'] == 'rotating' else int(cache.offset))
    object.__setattr__(cache, '_boundary_attention_summary', (identity, post))


def context_tracker_is_current(cache, info):
    """Checkpoint-time freshness proof from this actual cache's live tracker."""
    if not info or int(cache.offset) != info['query_position'] + 1:
        return False
    tracker = getattr(cache, '_boundary_attention_summary', None)
    rotating = hasattr(cache, 'max_size')
    identity = (int(cache.offset), id(cache.keys), id(cache.values),
                int(cache.max_size) if rotating else None, int(cache.keep) if rotating else 0,
                int(cache._idx) if rotating else int(cache.offset))
    return tracker is not None and tracker[0] == identity


def restore_context_tracker(cache, arrays, info, xp=np, *, confirmed_current=False):
    """Resume aggregate arithmetic exactly when the saved boundary is current.

    Complete checkpoints already contain native cache state. Recreate the
    disposable post-query tracker using the saved pre-query moments and that
    cache's current cell, without adding a token/KV log or new persisted arrays.
    Older/unobserved boundaries deliberately fall back to a fresh reduction.
    Offset equality alone is insufficient after ordinary-state reset: callers
    must pass a checkpoint-time freshness marker from the original live cache.
    """
    if not confirmed_current or not info or int(cache.offset) != info['query_position'] + 1:
        return False
    rotating = hasattr(cache, 'max_size')
    if ((info['cache_kind'] == 'rotating') != rotating or
            (rotating and (cache.max_size != info['cache_capacity'] or cache.keep != info['cache_keep']))):
        return False
    summary = {key: xp.array(arrays['context.'+key], dtype=xp.float32) for key in (CROSS,MASS)}
    finish_context(cache,summary,info,xp)
    return True
