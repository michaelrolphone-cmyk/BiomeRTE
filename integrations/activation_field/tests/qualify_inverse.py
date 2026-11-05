"""Synthetic input recovery from a real 9B field checkpoint, no transcript input."""
import hashlib,json,tempfile
from pathlib import Path
from activation_field.checkpoint import Vault,canonical
from activation_field.config import FieldConfig
from activation_field.manifest import read_manifest
from activation_field.runtime import Engine
from activation_field.admission import model_slot
from activation_field.inverse_runtime import reconstruct

root=Path.home()/'.local/share/biome-activation-field'
manifest=read_manifest(root/'model-manifest.json')
result={'status':'running','checks':{}}
out=root/'inverse-qualification.json'
def publish():out.write_text(json.dumps(result,indent=2))
def check(name,condition):
    result['checks'][name]=bool(condition);publish()
    if not condition:raise AssertionError(name)
publish()
try:
    with tempfile.TemporaryDirectory(prefix='inverse-qualification-',dir=root) as temp:
        vault=Vault(temp)
        with model_slot():
            engine=Engine(manifest,FieldConfig(enabled=True,spectral_enabled=False))
            try:
                ids=engine.tokenizer.encode(' jade',add_special_tokens=False)
                check('synthetic_source_is_one_token',len(ids)==1)
                for token in ids:engine.consume(token)
                meta,tensors=engine.state(field_only=True);vault.save('source',meta,tensors);tensors=None
                check('source_changed_packed_weights',engine.field.changed_words>0)
                original=hashlib.sha256(canonical(vault.manifest('source'))).hexdigest()
            finally:engine.close()
        reconstructed=reconstruct(manifest,vault,'source',dict(max_tokens=1,candidate_pool=4,beam_width=2,sketch_rank=1,power_iterations=4))
        result['result']=reconstructed;result['synthetic_expected_token_ids']=ids;publish()
        check('source_checkpoint_unchanged',original==hashlib.sha256(canonical(vault.manifest('source'))).hexdigest())
        check('no_transcript_cache_or_logits_used',all(reconstructed[key] is False for key in ('uses_source_transcript','uses_source_native_caches','uses_source_logits')))
        check('full_model_replay_coverage',all(c['scored_layers']==32 and c['scored_projections']==248 for c in reconstructed['candidates']))
        check('source_token_in_delta_proposals',ids[0] in [p['token_id'] for p in reconstructed['token_proposals']])
        check('synthetic_token_reconstructed',reconstructed['candidates'][0]['token_ids']==ids)
        check('reconstructed_replay_matches_delta',reconstructed['best_relative_squared_error']==0)
        check('candidate_remains_hypothesis',reconstructed['candidate_kind']=='hypothesis' and reconstructed['scientific_reconstruction_success']=='not_established')
        result['status']='passed';publish()
except Exception as exc:
    result.update(status='failed',error=type(exc).__name__+': '+str(exc));publish();raise
print(json.dumps({'status':result['status'],'checks':result['checks'],'best_relative_squared_error':result.get('result',{}).get('best_relative_squared_error')}))
