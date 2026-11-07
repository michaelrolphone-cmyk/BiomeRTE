"""Output-causal native mechanics, not a language-coherence qualification."""
from copy import deepcopy
from types import SimpleNamespace
import numpy as np
import pytest
from activation_field.inverse import InverseOptions


def test_explicit_sequence_and_legacy_readout_options():
    assert InverseOptions.parse().readout_mode=='independent'
    assert InverseOptions.parse({'readout_mode':'sequence'}).readout_mode=='sequence'
    assert InverseOptions.parse({'readout_mode':'independent'}).readout_mode=='independent'
    for bad in (None,True,'trained','',[],{}):
        with pytest.raises(ValueError):InverseOptions.parse({'readout_mode':bad})


mx=pytest.importorskip('mlx.core')
from test_evolving_reverse_native import captured
from activation_field.adapters.sequence_readout import CausalSequenceReadout,native_current_calls
from activation_field.adapters.qwen35_reverse import Qwen35ReverseAdapter
from activation_field.inverse_runtime import token_readout,reconstruct_with_adapter
from activation_field.reverse import reverse_operations
from activation_field.reverse_debug import ReverseDebug


class RecordingPartition:
    dimension=64
    def __init__(self,tokens):self.tokens=tokens;self.inputs=[]
    def readout(self,vector,trace=None):
        self.inputs.append(np.array(vector,copy=True))
        return self.tokens[(len(self.inputs)-1)%len(self.tokens)]


def setup():
    mx.random.seed(184)
    model,field,records=captured()
    engine=SimpleNamespace(model=model,field=field,config=field.config,
        manifest={'baseline_hash':'tiny-native-sequence'},tokenizer=SimpleNamespace(all_special_ids=[0]),
        cache='source-cache-must-not-be-used')
    return engine,records


def snapshot(records,field):
    return ({name:{key:np.array(value) for key,value in record.arrays.items()} for name,record in records.items()},
            {name:{key:np.array(value) for key,value in record.arrays.items()} for name,record in field.directions.items()})


def assert_snapshot(before,records,field):
    for saved,current in zip(before,(records,field.directions)):
        for name,arrays in saved.items():
            for key,value in arrays.items():np.testing.assert_array_equal(value,np.array(current[name].arrays[key]))


def test_same_reverse_vectors_depend_on_emitted_prefix_beyond_last_token():
    engine,records=setup();before=snapshot(records,engine.field)
    left,right=RecordingPartition([1,2,3]),RecordingPartition([4,2,3])
    a,b=[CausalSequenceReadout(engine,p,1.) for p in (left,right)]
    vector=np.arange(64,dtype=np.float32)-32
    for _ in range(3):a.readout(vector);b.readout(vector)
    np.testing.assert_array_equal(left.inputs[0],right.inputs[0])
    assert not np.allclose(left.inputs[1],right.inputs[1])
    # Both prefixes now end in token2, but their earlier token is different.
    assert not np.allclose(left.inputs[2],right.inputs[2])
    assert a.steps==b.steps==3 and engine.cache=='source-cache-must-not-be-used'
    assert_snapshot(before,records,engine.field)
    assert not engine.field.captured and not engine.field.boundary_captured
    assert not hasattr(a,'tokens') and not hasattr(a,'source_tokens')


def test_evolving_reverse_state_changes_next_causal_readout():
    engine,_=setup()
    left,right=RecordingPartition([1]),RecordingPartition([1])
    a,b=[CausalSequenceReadout(engine,p,1.) for p in (left,right)]
    x=np.eye(64,dtype=np.float32)
    a.readout(x[0]);b.readout(x[0]);a.readout(x[1]);b.readout(x[2])
    assert not np.allclose(left.inputs[1],right.inputs[1])


@pytest.mark.parametrize('weight_mode',['current','delta'])
def test_causal_readout_preserves_pending_reverse_data_and_context_advancement(weight_mode):
    engine,records=setup()
    readout=CausalSequenceReadout(engine,RecordingPartition([1]),1.)
    adapter=Qwen35ReverseAdapter(engine,records,readout,weight_mode=weight_mode)
    trajectory=adapter.begin(records,records,engine.field.entries)
    flow=reverse_operations(adapter.operations,adapter.signals(trajectory),records,
                            engine.field.entries,adapter.counterparts)
    stages=('last_output','last_incoming','last_conditioned','last_pullback')
    saved={path:{key:getattr(state,key) for key in stages} for path,state in trajectory.projections.items()}
    traces={i:layer.linear_attn._reverse_recurrence for i,layer in enumerate(engine.model.layers) if layer.is_linear}
    # Compute the expected context successor without any causal readout. The
    # detached context clone shares pending native tensors, never source arrays.
    expected={}
    for name,state in trajectory.layers.items():
        clone=deepcopy(state);clone.pending=state.pending
        adapter._advance_context(name,clone);expected[name]=clone.context
    for i,trace in traces.items():object.__setattr__(engine.model.layers[i].linear_attn,'_reverse_recurrence',trace)
    classes=[type(getattr(p,n)) for _,_,p,n,*_ in engine.field.entries]
    assert adapter.readout(flow,trajectory)==1
    assert classes==[type(getattr(p,n)) for _,_,p,n,*_ in engine.field.entries]
    for path,values in saved.items():
        for key,value in values.items():assert getattr(trajectory.projections[path],key) is value
    for i,trace in traces.items():assert engine.model.layers[i].linear_attn._reverse_recurrence is trace
    adapter.advance(trajectory,flow)
    for name,state in trajectory.layers.items():
        for key,value in state.context.items():np.testing.assert_array_equal(value,expected[name][key])


def test_delta_reverse_uses_current_weights_only_in_causal_path():
    engine,records=setup();partition=RecordingPartition([1]);readout=CausalSequenceReadout(engine,partition,1.)
    vector=np.arange(64,dtype=np.float32)
    readout.readout(vector);expected=partition.inputs[-1]
    readout.reset()
    adapter=Qwen35ReverseAdapter(engine,records,readout,weight_mode='delta')
    readout.readout(vector)
    np.testing.assert_array_equal(expected,partition.inputs[-1])
    assert all(getattr(module,'_delta_installed',False) and type(module).__name__.startswith('Delta')
               for _,module,*_ in engine.field.entries)
    assert adapter.weight_mode=='delta'


def test_classes_and_capture_restore_on_exception():
    engine,records=setup();Qwen35ReverseAdapter(engine,records,None,weight_mode='delta')
    modules=[m for _,m,*_ in engine.field.entries]+[getattr(p,n) for _,_,p,n,*_ in engine.field.entries]
    types=[type(m) for m in modules];capture=engine.field.capture
    with pytest.raises(RuntimeError,match='synthetic'):
        with native_current_calls(engine):raise RuntimeError('synthetic')
    assert types==[type(m) for m in modules] and engine.field.capture==capture


def test_output_partition_cache_is_separate_and_causal_calls_never_score_vocab(tmp_path):
    engine,records=setup()
    input_tree=token_readout(engine,tmp_path,table='input')
    output_tree=token_readout(engine,tmp_path,table='output')
    assert len(list(tmp_path.glob('*.npz')))==2
    cached=token_readout(engine,tmp_path,table='output')
    assert cached._nodes==output_tree._nodes
    # Native output head is deliberately poisonous. Hidden-only causal calls
    # and cached tree loading must not invoke it or a tied vocabulary projection.
    head=engine.model.lm_head
    original=type(head)
    class NoVocabulary(original):
        def __call__(self,*args,**kwargs):raise AssertionError('vocabulary sweep')
    object.__setattr__(head,'__class__',NoVocabulary)
    readout=CausalSequenceReadout(engine,cached,1.)
    adapter=Qwen35ReverseAdapter(engine,records,readout)
    result=reconstruct_with_adapter(adapter,records,records,engine.field.entries,str)
    assert result['estimated_token_ids']
    assert all(token!=0 for token in result['estimated_token_ids'])
    assert len(result['estimated_token_ids'])==readout.steps


def test_output_caches_reset_and_debug_is_observational(tmp_path):
    engine,records=setup();partition=token_readout(engine,tmp_path,table='output')
    source=snapshot(records,engine.field)
    readout=CausalSequenceReadout(engine,partition,1.)
    adapter=Qwen35ReverseAdapter(engine,records,readout)
    plain=reconstruct_with_adapter(adapter,records,records,engine.field.entries,str)
    repeated=reconstruct_with_adapter(adapter,records,records,engine.field.entries,str)
    assert plain==repeated
    debug=ReverseDebug();readout.debug=debug
    traced=Qwen35ReverseAdapter(engine,records,readout,debug=debug)
    actual=reconstruct_with_adapter(traced,records,records,engine.field.entries,str,debug=debug)
    report=actual.pop('debug');assert actual==plain
    assert 'sequence.causal_hidden' in report['signal_names']
    assert 'sequence.previous_embedding' in report['signal_names']
    assert_snapshot(source,records,engine.field)


def test_existing_output_cache_window_is_not_an_emission_limit():
    engine,_=setup();engine.config.context_length=2
    readout=CausalSequenceReadout(engine,RecordingPartition([1,2,3]),1.)
    for _ in range(11):readout.readout(np.ones(64,dtype=np.float32))
    assert readout.steps==11
    for layer,cache in zip(engine.model.layers,readout.cache):
        if not layer.is_linear:assert cache.offset==11 and cache.keys.shape[2]==2


def test_actual_qwen_model_wrapper_uses_untied_output_rows(tmp_path):
    from dataclasses import asdict
    from mlx_lm.models.qwen3_5 import Model,ModelArgs
    from activation_field.token_readout import PartitionTokenReadout
    engine,records=setup()
    wrapper=Model(ModelArgs(model_type='qwen3_5',text_config=asdict(engine.model.args)))
    wrapper.language_model=engine.model
    engine.model=wrapper
    actual=token_readout(engine,tmp_path,table='output')
    head=wrapper.language_model.lm_head
    rows=np.asarray(mx.dequantize(head.weight,head.scales,head.biases,
                                  group_size=head.group_size,bits=head.bits,mode=head.mode).astype(mx.float32))[1:]
    rows/=np.maximum(np.linalg.norm(rows,axis=1,keepdims=True),1e-12)
    expected=PartitionTokenReadout.from_embeddings(rows,token_ids=np.arange(1,64),normalize=False)
    assert actual._nodes==expected._nodes
    readout=CausalSequenceReadout(engine,actual,1.)
    adapter=Qwen35ReverseAdapter(engine,records,readout)
    result=reconstruct_with_adapter(adapter,records,records,engine.field.entries,str)
    assert result['estimated_token_ids']


def test_untied_missing_head_is_not_silently_replaced_by_input_embeddings(tmp_path):
    engine,_=setup()
    del engine.model['lm_head']
    with pytest.raises(ValueError,match='missing its output head'):
        token_readout(engine,tmp_path,table='output')
