"""Causal readout using generated output only, with shared current Qwen weights.

This is an untrained soft-conditioning experiment. It adds genuine ordered
output dependence, not a learned semantic decoder for the reverse snapshot.
The native vocabulary head is never called; selection follows one partition
path over frozen output-head rows.
"""
from contextlib import contextmanager
import numpy as np
import mlx.core as mx
from mlx_lm.models.cache import ArraysCache, RotatingKVCache
from ..directions import normalized


@contextmanager
def native_current_calls(engine):
    """Bypass private reverse wrappers without copying/changing model tensors.

    Readout runs before reverse advancement. In particular it must NOT replace
    pending recurrence arguments or projection outputs that advancement needs.
    Every original class is restored even if evaluation raises or is cancelled.
    Training flags remain untouched; Qwen's reference recurrence is also valid
    for ordinary forward evaluation.
    """
    changed=[]
    capture=engine.field.capture
    engine.field.capture=False
    try:
        modules=[]
        for _,module,parent,name,*_ in engine.field.entries:
            modules.append(getattr(parent,name))
            if getattr(module,'_delta_installed',False):
                base=getattr(module,'_delta_base_type',None)
                if base is None:raise ValueError('Delta module lacks its native counterpart')
                changed.append((module,type(module)))
                object.__setattr__(module,'__class__',base)
        for layer in engine.model.layers:
            modules.append(layer.linear_attn if layer.is_linear else layer.self_attn)
        for module in modules:
            base=getattr(module,'_reverse_base_type',None)
            if base is not None:
                changed.append((module,type(module)))
                object.__setattr__(module,'__class__',base)
        yield
    finally:
        for module,kind in reversed(changed):object.__setattr__(module,'__class__',kind)
        engine.field.capture=capture


class CausalSequenceReadout:
    """Separate output-only caches; no source token history or source caches."""
    kind='causal_output_head_partition'
    weight_mode='current'

    def __init__(self,engine,partition,input_scale,debug=None,check=lambda:None):
        self.engine,self.partition=engine,partition
        self.input_scale=float(input_scale)
        if not np.isfinite(self.input_scale) or self.input_scale<0:
            raise ValueError('Invalid sequence input scale')
        self.debug,self.check=debug,check
        self.reset()

    def reset(self):
        config=getattr(self.engine,'config',self.engine.field.config)
        self.cache=[ArraysCache(size=2) if layer.is_linear else
                    RotatingKVCache(config.context_length,keep=0)
                    for layer in self.engine.model.layers]
        self.previous_token=None
        self.steps=0

    def readout(self,reverse_vector,trace=None):
        self.check()
        value=np.asarray(reverse_vector,dtype=np.float32)
        if value.shape!=(self.partition.dimension,) or not np.isfinite(value).all():
            raise ValueError('Sequence reverse signal must be a finite embedding vector')
        direction=normalized(value)
        previous=np.zeros_like(direction)
        if self.previous_token is None:
            # The saved aggregate input magnitude is the only source scale
            # available. A zero aggregate has no measured amplitude; unit scale
            # keeps its directional signal defined without inventing history.
            joint=direction*(self.input_scale or 1.)
        else:
            embedding=self.engine.model.model.embed_tokens(mx.array([[self.previous_token]]))
            mx.eval(embedding)
            previous=np.asarray(embedding.astype(mx.float32)).reshape(-1)
            # Equal soft conditioning in the actual embedding coordinates. This
            # mixing choice is explicit and untrained, not source recovery.
            joint=.5*(previous+float(np.linalg.norm(previous))*direction)
        if self.debug is not None:
            self.debug.observe('sequence.reverse_input',direction)
            self.debug.observe('sequence.previous_embedding',previous)
            self.debug.observe('sequence.conditioned_input',joint)
        with native_current_calls(self.engine):
            # Call only the hidden-state backbone, never TextModel.__call__ or
            # lm_head: neither vocabulary logits nor candidates are evaluated.
            hidden=self.engine.model.model(None,cache=self.cache,
                        input_embeddings=mx.array(joint).reshape(1,1,-1))
            mx.eval(hidden,*[array for cache in self.cache for array in cache.state if array is not None])
        self.check()
        hidden=np.asarray(hidden.astype(mx.float32)).reshape(-1)
        if not np.isfinite(hidden).all():raise FloatingPointError('Nonfinite causal readout state')
        if self.debug is not None:self.debug.observe('sequence.causal_hidden',hidden)
        token=self.partition.readout(normalized(hidden),trace=trace)
        self.previous_token=token
        self.steps+=1
        return token
