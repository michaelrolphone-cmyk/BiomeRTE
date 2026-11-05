"""Full-model regression for batched commits and metrics at real boundaries."""
import json,tempfile
from pathlib import Path
import numpy as np
import mlx.core as mx
from activation_field.runtime import Engine
from activation_field.config import FieldConfig
from activation_field.manifest import read_manifest
from activation_field.checkpoint import Vault
from activation_field.admission import model_slot
from activation_field.measurements import field_drift
from experiments.storage import atomic_json

root=Path.home()/'.local/share/biome-activation-field'
out=root/'continuation-boundaries.json';checks={}
def check(k,v):
    checks[k]=bool(v);atomic_json(out,dict(status='running',checks=checks))
    if not v:raise AssertionError(k)
try:
    with model_slot():
        e=Engine(read_manifest(root/'model-manifest.json'),FieldConfig(enabled=True,commit_every_tokens=8,spectral_enabled=False))
        try:
            ids=e.tokenizer.encode('Synthetic marker: jade.',add_special_tokens=False)
            for t in ids[:2]:e.consume(t)
            check('batch_has_pending_tail',len(e.field.pending)==2 and e.field.version==0)
            e.reset('ordinary_state')
            check('ordinary_reset_commits_tail',not e.field.pending and e.field.version==1 and e.field.changed_words>0)
            check('all_native_caches_empty_after_reset',all(c.empty() for c in e.cache))
            receipt=e.field.receipts[-1]
            check('all_layers_have_measured_metrics',len(receipt['per_layer'])==32)
            check('actual_realized_norms',sum(x['realized_squared_norm'] for x in receipt['per_layer'])>0)
            check('actual_quantization_error',sum(x['quantization_squared_error'] for x in receipt['per_layer'])>0)
            check('finite_measured_norms',all(np.isfinite(x['proposal_squared_norm']) for x in receipt['per_layer']))
            with tempfile.TemporaryDirectory(prefix='af-continuation-',dir=root) as temp:
                vault=Vault(temp);meta,tensors=e.state();vault.save('good',meta,tensors);tensors=None
                before=e.field.version
                invalid=dict(meta);invalid.pop('receipts')
                vault.save('invalid',invalid,vault.arrays('good'))
                try:e.restore(vault,'invalid')
                except (KeyError,ValueError):pass
                else:raise AssertionError('bad checkpoint accepted')
                check('invalid_metadata_does_not_publish',e.field.version==before and e.config.enabled)
                e.patch_config({'enabled':False,'read_only':True})
                e.restore(vault,'good')
                check('restore_settings_roundtrip',e.config.enabled and e.config.commit_every_tokens==8)
                e.patch_config({'enabled':False,'read_only':True});before=e.field.changed_words
                e.consume(ids[0]);check('disabled_after_restore_does_not_write',e.field.changed_words==before)
            drift=field_drift(e.field)
            check('drift_all_layers_and_finite',len(drift)==32 and all(np.isfinite(x['relative_drift_percentiles']['p100']) for x in drift))
            result=dict(status='passed',checks=checks,drift=drift,diagnostics=e.diagnostics())
        finally:e.close()
except Exception as exc:
    atomic_json(out,dict(status='failed',checks=checks,error=type(exc).__name__+': '+str(exc)));raise
atomic_json(out,result);print(json.dumps(dict(status=result['status'],checks=checks)))
