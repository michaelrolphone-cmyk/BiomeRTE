"""Baseline-only restoration. Staged encrypted shards publish atomically."""
import hashlib
import time
from pathlib import Path
import numpy as np
from ..checkpoint import Vault
from .common import TransformError, digest, check_cancel
from .format import load_transform, inspect_transform
from .coordinates import tile_coordinates, quantize, pack
from .regions import evaluate_delta


def validate_baseline(source, manifest):
    from .format import SIGNED_RESTORATION
    if getattr(source,'has_negative_scales',False) and manifest['restoration_rule']!=SIGNED_RESTORATION:
        raise TransformError('unsupported_layout','Negative native scales require the signed-scale restoration rule')
    if source.baseline_ref != manifest['baseline_ref']:
        raise TransformError('baseline_mismatch', 'Artifact requires its pinned original model')
    actual={d['path']:d for d in source.descriptors}
    if set(actual)!={d['path'] for d in manifest['tensor_map']}:
        raise TransformError('unsupported_layout','Tensor coverage mismatch')
    for d in manifest['tensor_map']:
        for key in ('id','logical_shape','orientation','dtype','frozen','quantization_ref'):
            if d[key]!=actual[d['path']][key]:
                raise TransformError('unsupported_layout','Baseline tensor/scale mapping mismatch: '+d['path'])


def materialize(source, artifact_path, output_vault, checkpoint_id, staging, resume=False, progress=None, cancel=None):
    started=time.monotonic();manifest,regions=load_transform(artifact_path)
    validate_baseline(source,manifest)
    if source.target is not None:
        raise ValueError('Materializer must be given a baseline-only source')
    artifact=inspect_transform(artifact_path)
    identity=digest(dict(baseline=source.baseline_ref,artifact=artifact['sha256'],checkpoint_id=checkpoint_id))
    private=Vault(Path(staging)/'materialization')
    state=dict(identity=identity,completed=[],audit={},elapsed_seconds=0.)
    if (private.root/'progress.enc').exists():
        if not resume:raise FileExistsError('Use explicit resume for materialization')
        state=private.load_json('progress.enc')
        if state['identity']!=identity:raise TransformError('source_version_changed','Materialization source changed')
    if (output_vault.root/checkpoint_id).exists():
        existing=output_vault.manifest(checkpoint_id)
        if existing['metadata'].get('transform_identity')!=identity:
            raise TransformError('invalid_artifact','Destination checkpoint already exists')
        return dict(checkpoint_id=checkpoint_id,sha256=digest(existing),audit=state['audit'],reconciled=True)
    try:
        for d in manifest['tensor_map']:
            check_cancel(cancel)
            name='tensor-'+str(d['id'])
            if name in state['completed']:continue
            # Reconcile a committed fragment whose progress write was interrupted.
            if not (private.root/name).exists():
                indices=[];codes=[];audit=dict(changed_codes=0,clipped=0,erased=0,zero_scale=0)
                for t in source.tiles(d):
                    check_cancel(cancel)
                    row,col=tile_coordinates(d,t)
                    delta=evaluate_delta(regions,d,row,col,t['base'])
                    q,counts=quantize(t,delta)
                    audit['changed_codes']+=int(np.count_nonzero(q!=t['base_codes']))
                    for k,v in counts.items():audit[k]+=v
                    packed=pack(q).reshape(-1,8)
                    original=pack(t['base_codes']).reshape(-1,8)
                    selected=np.flatnonzero(np.any(packed!=original,axis=1))
                    if len(selected):
                        indices.append((selected+t['start']*(d['logical_shape'][1]//64)).astype(np.uint32))
                        codes.append(packed[selected])
                    if progress:progress(dict(stage='materialize',tensor=d['path'],row=t['start']))
                idx=np.concatenate(indices) if indices else np.empty(0,np.uint32)
                values=np.concatenate(codes) if codes else np.empty((0,8),np.uint32)
                private.save(name,dict(baseline_hash=source.baseline_ref['baseline_hash'],identity=identity,audit=audit),[('indices',idx),('codes',values)])
            fragment=private.manifest(name)
            if fragment['metadata']['identity']!=identity:raise TransformError('source_version_changed','Staging mismatch')
            state['audit'][d['path']]=fragment['metadata']['audit'];state['completed'].append(name)
            private.save_json('progress.enc',state)
        check_cancel(cancel)
        meta,_=source.engine.state(field_only=True)
        cfg=dict(meta['config'],enabled=False,read_only=True,baseline_pull=0.,spectral_enabled=False)
        meta.update(config=cfg,field_version=0,field_tokens=0,consumed_tokens=0,sample_counter=0,native_tokens=0,
                    config_revision=0,changed_words=0,receipts=[],prefix_hashes=[],cache=None,last_logits=None,requires_reset=False,
                    trajectory=dict(tokens=0,buffer=[],descriptors=[],window=cfg['window_tokens'],stride=cfg['stride_tokens']),
                    transform_identity=identity,source_transform=artifact['sha256'],source_transform_bytes=artifact['bytes'],
                    input_stream_digest=hashlib.sha256(b'').hexdigest(),input_digest_origin='functional_transform; original stream unknown')
        def tensors():
            for d in manifest['tensor_map']:
                if private.manifest('tensor-'+str(d['id']))['metadata']['identity']!=identity:
                    raise TransformError('source_version_changed','Committed fragment identity mismatch')
                fragment=dict(private.arrays('tensor-'+str(d['id'])))
                if len(fragment['indices']):
                    yield d['path']+'.indices',fragment['indices']
                    yield d['path']+'.codes',fragment['codes']
                yield d['path']+'.frequency',np.zeros(d['logical_shape'][0],np.float32)
        receipt=output_vault.save(checkpoint_id,meta,tensors())
        state['receipt']=receipt;state['status']='succeeded'
        return dict(**receipt,audit=state['audit'],artifact=artifact,restoration_seconds=time.monotonic()-started+state['elapsed_seconds'],native_state='empty',writes=False,decay=False)
    finally:
        state['elapsed_seconds']+=time.monotonic()-started
        private.save_json('progress.enc',state)
