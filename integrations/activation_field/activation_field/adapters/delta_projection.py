"""Private reverse-only quantized projections for ΔW = Wcurrent - W0.

Only eligible projection matrices change semantics. Frozen embeddings, norms,
convolutions, residual paths and recorded contexts retain their usual meaning.
No packed weights or quantization parameters are changed or dequantized here.
"""
import mlx.core as mx
import mlx.nn as nn


def install_delta_projection(module, baseline):
    if (not isinstance(module, nn.QuantizedLinear) or module.bits != 4
            or module.group_size != 64 or module.mode != 'affine'):
        raise ValueError('Delta reverse requires affine4 group64 projections')
    if getattr(module, '_delta_installed', False):
        raise ValueError('Delta counterpart already installed')
    if baseline.shape != module.weight.shape or baseline.dtype != mx.uint32:
        raise ValueError('Invalid packed projection baseline')
    # Scales, quantization offsets and ordinary bias are immutable in the field.
    # Capture their existing references; never register a second parameter tree.
    scales, offsets = module.scales, module.biases

    def call(self, x):
        # Compute both products in FP32, not just their final subtraction: BF16
        # rounding of the large baseline products can erase small signed deltas.
        settings = dict(scales=scales.astype(mx.float32),
                        biases=offsets.astype(mx.float32), transpose=True,
                        group_size=64, bits=4, mode='affine')
        value = x.astype(mx.float32)
        current = mx.quantized_matmul(value, self.weight, **settings)
        original = mx.quantized_matmul(value, baseline, **settings)
        # Both branches participate in the VJP. Frozen ordinary linear bias
        # cancels as well: (x Wcurrent^T + b) - (x W0^T + b).
        return (current - original).astype(x.dtype)

    private = type('Delta'+type(module).__name__, (type(module),), {'__call__': call})
    object.__setattr__(module, '_delta_base_type', type(module))
    object.__setattr__(module, '__class__', private)
    object.__setattr__(module, '_delta_installed', True)


def install_delta_engine(engine):
    """Install only on the private reverse engine, preserving Tap execution."""
    entries = engine.field.entries
    for path, module, parent, name, *_ in entries:
        executing = getattr(parent, name)
        if getattr(executing, 'linear', executing) is not module:
            raise ValueError('Projection is not the executing module: '+path)
        baseline = engine.field.anchor[path]
        if (not isinstance(module, nn.QuantizedLinear) or module.bits != 4
                or module.group_size != 64 or module.mode != 'affine'
                or baseline.shape != module.weight.shape or baseline.dtype != mx.uint32
                or getattr(module, '_delta_installed', False)):
            raise ValueError('Invalid delta projection: '+path)
    for path, module, *_ in entries:
        install_delta_projection(module, engine.field.anchor[path])
    return len(entries)
