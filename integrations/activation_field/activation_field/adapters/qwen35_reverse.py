"""Numerical approximate counterparts of the installed Qwen3.5 operations.

Layer vector-Jacobian products carry a signed signal backwards through the actual
normalization, residual, nonlinear, attention, convolution and recurrent
operations at aggregate boundary state, using the current adjusted weights.
They are not exact inverses and do not optimize a match to source activations.
"""
import numpy as np
import mlx.core as mx
from types import FunctionType
from mlx_lm.models.cache import ArraysCache, KVCache
from ..boundaries import ReverseTrajectory
from ..directions import normalized
from ..reverse import Operation, ReverseUnavailable, combine_write_moments


def _reference_attention(queries,keys,values,*,cache,scale,mask):
    """Differentiable grouped attention for the private aggregate reverse call.

    MLX's single-query vector SDPA kernel has no VJP for some real head sizes.
    Use the same attention arithmetic with ordinary differentiable primitives.
    The reverse call has one aggregate cell plus the current cell, no mask.
    """
    if mask is not None:
        raise ValueError('Aggregate reverse attention expects no mask')
    batch,heads,length,dimension=queries.shape
    kv_heads=keys.shape[1]
    if heads % kv_heads:
        raise ValueError('Invalid grouped attention heads')
    q=queries.reshape(batch,kv_heads,heads//kv_heads,length,dimension)
    k=mx.expand_dims(keys,2)
    v=mx.expand_dims(values,2)
    scores=(q*scale) @ mx.swapaxes(k,-1,-2)
    probabilities=mx.softmax(scores.astype(mx.float32),axis=-1).astype(q.dtype)
    return (probabilities @ v).reshape(batch,heads,length,values.shape[-1])


def _install_reverse_attention(attention):
    """Keep the installed attention body, changing only its private SDPA binding."""
    original=type(attention).__call__
    if 'scaled_dot_product_attention' not in original.__globals__:
        raise ReverseUnavailable('attention_counterpart_unavailable',
                                 'Installed Qwen attention has an unsupported implementation')
    namespace=dict(original.__globals__,scaled_dot_product_attention=_reference_attention)
    call=FunctionType(original.__code__,namespace,original.__name__,
                      original.__defaults__,original.__closure__)
    call.__kwdefaults__=original.__kwdefaults__
    # A transparent subclass keeps all parameters, projections, norms, RoPE,
    # cache handling, output gate and parameter paths unchanged. No global patch.
    private=type('Reverse'+type(attention).__name__,(type(attention),),{'__call__':call})
    object.__setattr__(attention,'__class__',private)


class Qwen35ReverseAdapter:
    def __init__(self, engine, records, token_readout, debug=None):
        self.engine=engine
        self.records=records
        self.token_readout=token_readout
        self.debug=debug
        self.operations=[]
        self.counterparts={}
        for i,layer in enumerate(engine.model.layers):
            if not layer.is_linear:
                _install_reverse_attention(layer.self_attn)
            name='layer.'+str(i)
            self.operations.append(Operation(name,('state.'+str(i),),'state.'+str(i+1)))
            self.counterparts[name]=self._layer_counterpart(layer,i)
        self.operations.append(Operation('final_norm',('state.'+str(len(engine.model.layers)),),'present'))
        self.counterparts['final_norm']=self._norm_counterpart
        expected={op.name for op in self.operations}
        if set(records)!=expected:
            raise ReverseUnavailable('operation_state_missing',
                                     'Missing or unexpected Qwen operation boundaries: '+str(sorted(expected^set(records))))
        # Reference recurrent operations support differentiation; this changes
        # only the private reverse engine's execution path, never its weights.
        engine.field.capture=False
        engine.model.train(True)

    def _layer_counterpart(self,layer,index):
        def apply(signal,record,weights):
            x=mx.array(record.arrays['input_mean']).reshape(1,1,-1)
            # The two branch outputs share hidden-state coordinates. Their
            # accumulated signed write directions condition the local signal.
            family='linear_attn' if layer.is_linear else 'self_attn'
            attention_output='out_proj' if layer.is_linear else 'o_proj'
            writes={}
            for suffix in (family+'.'+attention_output,'mlp.down_proj'):
                path=f'language_model.model.layers.{index}.'+suffix
                projection=self.engine.field.directions.get(path)
                if projection is not None:
                    writes[suffix]=np.asarray(projection.arrays['write_mean'])
            combined=combine_write_moments(signal,writes.values())
            if self.debug is not None:
                write_norms={name:float(np.linalg.norm(write.astype(np.float64))) for name,write in writes.items()}
                bias=combine_write_moments(np.zeros_like(signal),writes.values())
                self.debug.conditioning('layer.'+str(index),signal,combined,write_norms,bias)
                self.debug.observe('layer.'+str(index)+'.conditioned',normalized(combined))
            incoming=mx.array(normalized(combined)).reshape(x.shape)
            if layer.is_linear:
                cache=ArraysCache(size=2)
                context=record.arrays
                # One aggregate convolution context, repeated across the kernel
                # slots. No original convolution-token window is stored/read.
                cache[0]=mx.repeat(mx.array(context['context.conv_mean']),layer.linear_attn.conv_kernel_size-1,axis=1)
                cache[1]=mx.array(context['context.recurrent'])
            else:
                cache=KVCache()
                # A single aggregate memory cell stands for the attended past.
                cache.update_and_fetch(mx.array(record.arrays['context.key_mean']),
                                       mx.array(record.arrays['context.value_mean']))
                # KVCache offset is also its physical length. Retain one cell;
                # absolute rotary phase is intentionally not reconstructed.
            _,gradients=mx.vjp(lambda value:layer(value,cache=cache),[x],[incoming])
            value=gradients[0].reshape(-1).astype(mx.float32)
            mx.eval(value)
            if self.debug is not None:self.debug.observe('layer.'+str(index)+'.pullback',np.asarray(value))
            return {'state.'+str(index):np.asarray(value)}
        return apply

    def _norm_counterpart(self,signal,record,weights):
        # Undo the learned per-coordinate scale, retaining only direction.
        # A norm VJP would remove a radial signal; it is not a useful counterpart
        # for the terminal normalized state. No original magnitude is recovered.
        weight=np.asarray(self.engine.model.model.norm.weight.astype(mx.float32))
        scaled=np.divide(signal,weight,out=np.zeros_like(signal),where=np.abs(weight)>1e-12)
        return {'state.'+str(len(self.engine.model.layers)):normalized(scaled)}

    def begin(self,present_state,directions,weights):
        return ReverseTrajectory(self.records['final_norm'],debug=self.debug)

    def finished(self,trajectory):return trajectory.done
    def signals(self,trajectory):return {'present':trajectory.value}
    def readout(self,flow,trajectory):
        if self.debug is None:return self.token_readout.readout(flow['state.0'])
        return self.token_readout.readout(flow['state.0'],
            trace=self.debug.readout)
    def advance(self,trajectory,flow):
        trajectory.advance()
        return trajectory
