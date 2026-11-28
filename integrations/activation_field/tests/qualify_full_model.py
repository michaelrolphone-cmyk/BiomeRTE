"""Real 9B/32-layer engineering qualification; synthetic inputs only."""
import json,sys,time,tempfile,hashlib
from pathlib import Path
import numpy as np
import mlx.core as mx
from mlx.utils import tree_flatten
from activation_field.manifest import read_manifest
from activation_field.runtime import Engine
from activation_field.config import FieldConfig
from activation_field.checkpoint import Vault
from activation_field.admission import model_slot
from activation_field.changes import FieldStore

root=Path.home()/'.local/share/biome-activation-field'
result={'status':'running','tests':{},'model':'Qwen3.5-9B','layers':32}
out=root/'qualification.json'
def publish():out.write_text(json.dumps(result,indent=2))
def checked(name,value):
    result['tests'][name]=bool(value);publish()
    if not value:raise AssertionError(name)
def digest(tensors):
    h=hashlib.sha256()
    for key,a in tensors:
        h.update(key.encode())
        if a.dtype==mx.bfloat16:a=a.view(mx.uint16)
        for start in range(0,a.shape[0] if a.ndim else 1,64):h.update(np.asarray(a[start:start+64] if a.ndim else a).tobytes())
    return h.hexdigest()
publish()
try:
    m=read_manifest(root/'model-manifest.json')
    with model_slot():
        e=Engine(m,FieldConfig(spectral_enabled=False));result['memory']=e.memory_estimate;publish()
        try:
            checked('all_32_layers_248_projections',len(e.field.entries)==248 and len(set(x[4] for x in e.field.entries))==32)
            frozen=[(p,a) for p,a in tree_flatten(e.model.parameters()) if not any(p==path+'.linear.weight' for path,*_ in e.field.entries)]
            before=digest(frozen);ids=e.tokenizer.encode('A synthetic marker is jade.',add_special_tokens=False)[:2]
            e.field.capture=False;cache=e.make_cache();plain=None
            for token in ids:plain=e.model(mx.array([[token]]),cache=cache)[0,-1];mx.eval(plain)
            e.field.capture=True;cache=None
            start=time.monotonic()
            for token in ids:trace=e.consume(token)
            result['disabled_seconds_per_token']=(time.monotonic()-start)/len(ids)
            error=float(mx.max(mx.abs(plain.astype(mx.float32)-trace)).item());result['disabled_max_logit_error']=error
            checked('field_disabled_reference_equivalence',error==0)
            e.sample_counter=7;e.reset('entire_session')
            checked('entire_reset_clears_sampling_and_token_counters',e.sample_counter==0 and e.consumed_tokens==0)
            e.patch_config({'enabled':True})
            anchor=e.field.anchor
            class Forbidden(dict):
                def __getitem__(self,key):raise AssertionError('Zero pull accessed baseline')
            e.field.anchor=Forbidden()
            start=time.monotonic()
            for token in ids:e.consume(token)
            result['enabled_seconds_per_token']=(time.monotonic()-start)/len(ids)
            e.field.anchor=anchor
            checked('zero_pull_bypasses_anchor',True)
            checked('actual_packed_weight_changes',e.field.changed_words>0)
            checked('frozen_tensors_unchanged',digest(frozen)==before)
            with tempfile.TemporaryDirectory(prefix='af-qualification-',dir=root) as tmp:
                vault=Vault(tmp);e.patch_config({'read_only':True,'enabled':False})
                meta,tensors=e.state();vault.save('resume',meta,tensors);tensors=None
                checked('opaque_stream_and_source_provenance',len(meta['input_stream_digest'])==64 and meta['input_stream_digest']!=hashlib.sha256(b'').hexdigest() and meta['source_revision'].startswith('sha256:'))
                weights_before=digest([(p,mod.weight) for p,mod,*_ in e.field.entries])
                freq_before=digest(list(e.field.frequency.items()))
                e.reset('ordinary_state');updated=e.consume(ids[0]);mx.eval(updated)
                checked('readonly_preserves_frequency',digest(list(e.field.frequency.items()))==freq_before)
                e.reset('weights');baseline=e.consume(ids[0]);mx.eval(baseline)
                effect=float(mx.max(mx.abs(updated-baseline)).item());result['max_later_logit_effect']=effect
                checked('weight_changes_affect_later_inference',effect>0)
                e.restore(vault,'resume')
                checked('complete_checkpoint_weights_roundtrip',digest([(p,mod.weight) for p,mod,*_ in e.field.entries])==weights_before)
                checked('complete_checkpoint_native_state',e.native_tokens==meta['native_tokens'] and all(not c.empty() for c in e.cache))
                checked('stream_identity_checkpoint_roundtrip',e.input_stream_digest==meta['input_stream_digest'])
                next_a=np.array(e.consume(ids[1]));e.restore(vault,'resume')
                next_b=np.array(e.consume(ids[1]))
                checked('complete_resume_next_logits_bitexact',np.array_equal(next_a,next_b))
                e.restore(vault,'resume')
                e.patch_config({'enabled':True,'read_only':False});e.reset('entire_session')
                for token in ids:e.consume(token)
                checked('same_seed_replay_bitexact',digest([(p,mod.weight) for p,mod,*_ in e.field.entries])==weights_before)
                e.patch_config({'enabled':False,'read_only':True});e.restore(vault,'resume')
                store=FieldStore(e,vault)
                change=store.export_changes(store.iter_changes(m['baseline_hash']),destination='map',protection_policy={'encrypted':True,'source_run_ref':'engineering-qualification'})
                materialized=store.materialize_state(m['baseline_hash'],'map','materialized')
                e.reset('entire_session');e.restore(vault,'materialized',reset_native_state=True)
                checked('change_map_bitexact_roundtrip',digest([(p,mod.weight) for p,mod,*_ in e.field.entries])==weights_before)
                checked('import_resets_all_native_caches',all(c.empty() for c in e.cache))
            result['diagnostics']=e.diagnostics();result['status']='passed';publish()
        finally:e.close()
except Exception as exc:
    result['status']='failed';result['error']=type(exc).__name__+': '+str(exc);publish();raise
print(json.dumps(result,indent=2))

