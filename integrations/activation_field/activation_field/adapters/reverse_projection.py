"""Private projection counterparts with operation-local aggregate boundaries.

The forward value is an equal blend of native and recorded directions at the
native magnitude. The reverse rule is deliberately a surrogate: actual selected
weight transpose of (incoming + signed write). It is not the derivative of the
boundary blend, nor an exact inverse. All gate/QKV/a/b/z/MLP projections use it.
"""
import numpy as np
import mlx.core as mx


def install_projection(executing, state_getter, path, debug_getter):
    # Reusing the private engine in tests must replace, not stack, counterparts.
    base = getattr(executing, '_reverse_base_type', type(executing))
    original = getattr(executing, '_reverse_base_call', base.__call__)

    def call(self, x):
        state = state_getter()
        if state is None:return original(self, x)
        write = mx.array(state.write).reshape((1,)*(x.ndim-1)+(-1,))

        def anchored(value, direction):
            shape = (1,)*(value.ndim-1)+(-1,)
            scale = mx.linalg.norm(value.astype(mx.float32), axis=-1, keepdims=True)
            return ((value + scale*mx.array(direction).reshape(shape))*.5).astype(value.dtype)

        @mx.custom_function
        def counterpart(value):
            local = anchored(value, state.input)
            result = anchored(original(self, local), state.output)
            state.last_output = result
            return result

        @counterpart.vjp
        def pullback(primals, cotangents, outputs):
            # Writes are bounded measured coefficients. Never normalize them
            # individually, including on a weak or cancelled projection.
            conditioned = cotangents + write.astype(cotangents.dtype)
            _, gradients = mx.vjp(lambda value: original(self, value),
                                   [primals], [conditioned])
            state.last_incoming = cotangents
            state.last_conditioned = conditioned
            state.last_pullback = gradients[0]
            return gradients[0]

        return counterpart(x)

    private = type('ReverseProjection'+base.__name__, (base,), {'__call__':call})
    object.__setattr__(executing, '__class__', private)
    object.__setattr__(executing, '_reverse_base_call', original)
    object.__setattr__(executing, '_reverse_base_type', base)


def observe_projection(path, state, debug):
    if debug is None:return
    values = [state.last_incoming, state.last_conditioned, state.last_pullback]
    mx.eval(*values)
    incoming, combined, pullback = [np.asarray(v, np.float32).reshape(-1) for v in values]
    debug.observe(path+'.incoming', incoming)
    debug.observe(path+'.conditioned', combined)
    debug.observe(path+'.pullback', pullback)
    debug.observe(path+'.input_boundary', state.input)
    debug.observe(path+'.output_boundary', state.output)
    debug.conditioning(path, incoming, combined,
                       {path:float(np.linalg.norm(state.write))}, state.write)
