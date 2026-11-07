"""Numerical approximate counterparts of the installed Qwen3.5 operations.

Layer vector-Jacobian products carry a signed signal backwards through the actual
normalization, residual, nonlinear, attention, convolution and recurrent
operations at aggregate boundary state, using current adjusted weights or explicitly selected delta projections.
They are not exact inverses and do not optimize a match to source activations.
"""
import numpy as np
import mlx.core as mx
from types import FunctionType
from mlx_lm.models.cache import ArraysCache, KVCache
from ..reverse_state import (ModelTrajectory, remove_paired_cell, reverse_conv_mean,
                             regularized_predecessor)
from ..attention_summary import CROSS, MASS, paired_attention, validate_descriptor, positive_features
from ..directions import normalized
from ..reverse import Operation, ReverseUnavailable
from .reverse_projection import install_projection, observe_projection


def _reference_attention(queries,keys,values,*,cache,scale,mask):
    """Differentiable grouped attention for the private aggregate reverse call.

    MLX's single-query vector SDPA kernel has no VJP for some real head sizes.
    Use the same attention arithmetic with ordinary differentiable primitives.
    The reverse call has one aggregate cell plus the current cell, no mask.
    """
    if mask is not None:
        raise ValueError('Aggregate reverse attention expects no mask')
    if isinstance(cache,PairedSummaryCache):
        if not np.isclose(scale,queries.shape[-1]**-.5):
            raise ValueError('Unsupported paired attention scale')
        return paired_attention(queries,keys,values,cache.summary,xp=mx)
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


class PairedSummaryCache:
    """Private reverse cache: absolute RoPE phase, no invented KV sequence."""
    def __init__(self,record,state=None):
        validate_descriptor(record.context_metadata)
        self.offset=record.context_metadata['query_position'] if state is None else state.position
        self.summary={key:mx.array(record.arrays['context.'+key] if state is None else state.context[key])
                      for key in (CROSS,MASS)}

    def update_and_fetch(self,keys,values):
        if keys.shape[2] != 1 or values.shape[2] != 1:
            raise ValueError('Paired reverse attention requires one current cell')
        self.current_keys, self.current_values = keys, values
        self.offset+=1
        # Historical moments remain constants. The freshly computed K/V are
        # passed through the native body, keeping both in the differentiated path.
        return keys,values


def _install_reverse_attention(attention):
    """Keep the installed attention body, changing only its private SDPA binding."""
    base=getattr(attention,'_reverse_base_type',type(attention))
    original=getattr(attention,'_reverse_base_call',base.__call__)
    if 'scaled_dot_product_attention' not in original.__globals__:
        raise ReverseUnavailable('attention_counterpart_unavailable',
                                 'Installed Qwen attention has an unsupported implementation')
    namespace=dict(original.__globals__,scaled_dot_product_attention=_reference_attention)
    call=FunctionType(original.__code__,namespace,original.__name__,
                      original.__defaults__,original.__closure__)
    call.__kwdefaults__=original.__kwdefaults__
    # A transparent subclass keeps all parameters, projections, norms, RoPE,
    # cache handling, output gate and parameter paths unchanged. No global patch.
    private=type('Reverse'+base.__name__,(base,),{'__call__':call})
    object.__setattr__(attention,'__class__',private)
    object.__setattr__(attention,'_reverse_base_type',base)
    object.__setattr__(attention,'_reverse_base_call',original)


class Qwen35ReverseAdapter:
    def __init__(self, engine, records, token_readout, debug=None, weight_mode='current'):
        if weight_mode not in ('current', 'delta'):
            raise ValueError('Reverse weight_mode must be current or delta')
        self.weight_mode=weight_mode
        if weight_mode == 'delta':
            from .delta_projection import install_delta_engine
            install_delta_engine(engine)
        self.engine=engine
        self.records=records
        self.token_readout=token_readout
        self.debug=debug
        self.trajectory=None
        entries=engine.field.entries
        expected_paths={entry[0] for entry in entries}
        if set(engine.field.directions) != expected_paths:
            raise ReverseUnavailable('projection_state_missing',
                'Every executing projection requires its own direction record')
        self.projection_paths={i:[] for i in range(len(engine.model.layers))}
        for path,module,parent,name,index,*_ in entries:
            executing=getattr(parent,name)
            self.projection_paths[index].append(path)
            install_projection(executing,
                lambda path=path:None if self.trajectory is None else self.trajectory.projections[path],
                path, lambda:self.debug)
        self.operations=[]
        self.counterparts={}
        for i,layer in enumerate(engine.model.layers):
            if not layer.is_linear:
                _install_reverse_attention(layer.self_attn)
            else:
                self._install_recurrence_trace(layer.linear_attn)
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

    def _install_recurrence_trace(self, attention):
        base=getattr(attention,'_reverse_base_type',type(attention))
        original=getattr(attention, '_reverse_base_call', base.__call__)
        native=original.__globals__.get('gated_delta_update')
        if native is None:
            raise ReverseUnavailable('recurrence_counterpart_unavailable',
                                     'Installed Qwen recurrence is unsupported')
        def traced(*args, **kwargs):
            kwargs=dict(kwargs,use_kernel=False)
            output,state=native(*args,**kwargs)
            object.__setattr__(attention,'_reverse_recurrence',(native,args,kwargs,state))
            return output,state
        namespace=dict(original.__globals__,gated_delta_update=traced)
        call=FunctionType(original.__code__,namespace,original.__name__,
                          original.__defaults__,original.__closure__)
        call.__kwdefaults__=original.__kwdefaults__
        private=type('Reverse'+base.__name__,(base,),{'__call__':call})
        object.__setattr__(attention,'__class__',private)
        object.__setattr__(attention,'_reverse_base_call',original)
        object.__setattr__(attention,'_reverse_base_type',base)

    def _layer_counterpart(self,layer,index):
        name='layer.'+str(index)
        def apply(signal,record,weights):
            state=self.trajectory.layers[name]
            x=mx.array(state.input).reshape(1,1,-1)
            # Local movement changes this layer's conditioning independently of
            # terminal movement. Projection writes enter at their own outputs.
            combined=signal + state.trajectory.value-state.initial_output
            incoming=mx.array(normalized(combined)).reshape(x.shape)
            if self.debug is not None:
                self.debug.observe(name+'.conditioned',np.asarray(incoming))
                self.debug.observe(name+'.input_boundary',state.input)
                self.debug.observe(name+'.output_boundary',state.trajectory.value)
            if layer.is_linear:
                cache=ArraysCache(size=2)
                cache[0]=mx.repeat(mx.array(state.context['conv_mean']),
                                   layer.linear_attn.conv_kernel_size-1,axis=1)
                cache[1]=mx.array(state.context['recurrent'])
            elif record.context_metadata:
                cache=PairedSummaryCache(record,state)
            else:
                cache=KVCache()
                cache.update_and_fetch(mx.array(state.context['key_mean']),
                                       mx.array(state.context['value_mean']))
            _,gradients=mx.vjp(lambda value:layer(value,cache=cache),[x],[incoming])
            value=gradients[0].reshape(-1).astype(mx.float32)
            mx.eval(value)
            result=np.asarray(value)
            self.trajectory.pullbacks[name]=result
            state.pending=(layer,cache)
            if self.debug is not None:
                self.debug.observe(name+'.pullback',result)
                for path in self.projection_paths[index]:
                    observe_projection(path,self.trajectory.projections[path],self.debug)
                for key,context in state.context.items():
                    self.debug.observe(name+'.context.'+key,context)
            return {'state.'+str(index):result}
        return apply

    def _advance_context(self,name,state):
        if state.pending is None:return
        layer,cache=state.pending
        record=self.records[name]
        if layer.is_linear:
            a=layer.linear_attn
            native,args,kwargs,predicted=a._reverse_recurrence
            current=mx.array(state.context['recurrent'])
            residual=current-predicted
            # Differentiate only the installed recurrence's STATE transition.
            # No projection write injection or gate-history invention here.
            def transition(value):
                operands=list(args);operands[7]=value
                return native(*operands,**kwargs)[1]
            _,gradients=mx.vjp(transition,[current],[residual])
            mx.eval(gradients)
            state.context['recurrent']=regularized_predecessor(
                state.context['recurrent'],np.asarray(gradients[0]),
                record.arrays['context.recurrent'])
            path='language_model.model.layers.'+name.split('.')[1]+'.linear_attn.in_proj_qkv'
            newest=self.trajectory.projections[path].last_output
            mx.eval(newest)
            state.context['conv_mean']=reverse_conv_mean(state.context['conv_mean'],
                np.asarray(newest),a.conv_kernel_size-1,record.arrays['context.conv_mean'])
            object.__setattr__(a,'_reverse_recurrence',None)
        elif record.context_metadata:
            if state.has_predecessor_cell():
                # The original summary excludes the current cell. Infer the
                # predecessor cell at the predecessor rotary phase. A rotating
                # summary cannot restore already-evicted older cells.
                key=layer.self_attn.rope(cache.current_keys,offset=-1)
                phi=positive_features(key,xp=mx)
                mx.eval(phi,cache.current_values)
                state.context.update(remove_paired_cell(state.context,
                                     np.asarray(phi),np.asarray(cache.current_values)))
                state.past_count-=1
                if not state.past_count:
                    for key in (CROSS,MASS):state.context[key]=np.zeros_like(state.context[key])
            # In an evicted middle range, keep the surviving prefix aggregate
            # intact until the reverse query actually reaches that prefix.
            state.position=max(0,state.position-1)
        else:
            # Schema-v1 has one mean cell, no paired mass or absolute phase.
            # Transport that cell toward the inferred local K/V without
            # upgrading it to a fictitious paired/history representation.
            keys,values=cache.keys,cache.values
            mx.eval(keys,values)
            for key,value in (('key_mean',keys),('value_mean',values)):
                current=state.context[key]
                newest=np.asarray(value[:,:,-1:,:])
                state.context[key]=regularized_predecessor(current,current-newest,
                                                          record.arrays['context.'+key])
        for value in state.context.values():
            if not np.isfinite(value).all():raise FloatingPointError('Nonfinite reverse context: '+name)
        state.pending=None

    def _norm_counterpart(self,signal,record,weights):
        # Undo the learned per-coordinate scale, retaining only direction.
        # A norm VJP would remove a radial signal; it is not a useful counterpart
        # for the terminal normalized state. No original magnitude is recovered.
        weight=np.asarray(self.engine.model.model.norm.weight.astype(mx.float32))
        scaled=np.divide(signal,weight,out=np.zeros_like(signal),where=np.abs(weight)>1e-12)
        return {'state.'+str(len(self.engine.model.layers)):normalized(scaled)}

    def begin(self,present_state,directions,weights):
        if hasattr(self.token_readout,'reset'):self.token_readout.reset()
        self.trajectory=ModelTrajectory(self.records,self.engine.field.directions,debug=self.debug)
        return self.trajectory

    def finished(self,trajectory):return trajectory.done
    def signals(self,trajectory):return {'present':trajectory.value}
    def readout(self,flow,trajectory):
        if self.debug is None:return self.token_readout.readout(flow['state.0'])
        return self.token_readout.readout(flow['state.0'],
            trace=self.debug.readout)
    def advance(self,trajectory,flow):
        for name,state in trajectory.layers.items():
            self._advance_context(name,state)
            state.advance(trajectory.pullbacks[name])
        for projection in trajectory.projections.values():
            projection.advance()
            # No lazy forward/VJP graph survives an emission boundary. Only
            # evaluated NumPy aggregate state and fixed source records remain.
            for key in ('last_output','last_incoming','last_conditioned','last_pullback'):
                if hasattr(projection,key):delattr(projection,key)
        if not trajectory.terminal.done:trajectory.terminal.advance()
        return trajectory
