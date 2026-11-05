"""Streaming extension seam for deferred domain-transform experiments."""
import hashlib,uuid,json
import numpy as np
import mlx.core as mx
from .quantized_writer import unpack


class FieldStore:
    def __init__(self,engine,vault):self.engine=engine;self.vault=vault

    def enumerate_state(self,checkpoint_ref=None,selection=None):
        for path,m,*_ in self.engine.field.entries:
            if selection and not any(path.startswith(prefix) for prefix in selection):continue
            yield dict(module_path=path,tensor_name='weight',logical_shape=[m.weight.shape[0],m.weight.shape[1]*8],packed_shape=list(m.weight.shape),dtype='uint32',quantization=dict(mode='affine',bits=4,group_size=64),baseline_ref=self.engine.manifest['baseline_hash'],tensor_map_version=1)

    def iter_changes(self,baseline_ref,field_ref=None,selection_policy=None):
        if baseline_ref!=self.engine.manifest['baseline_hash']:raise ValueError('Baseline mismatch')
        policy=selection_policy or {};threshold=float(policy.get('threshold',0))
        if not np.isfinite(threshold) or threshold<0:raise ValueError('Invalid threshold')
        if field_ref:
            metadata=self.vault.manifest(field_ref)['metadata']
            if metadata['baseline_hash']!=baseline_ref:raise ValueError('Baseline mismatch')
            entries={p:m for p,m,*_ in self.engine.field.entries};indices={}
            for key,a in self.vault.arrays(field_ref):
                if key.endswith('.indices'):indices[key[:-8]]=a
                elif key.endswith('.codes'):
                    path=key[:-6];m=entries[path];all_idx=indices.pop(path)
                    for start in range(0,len(all_idx),4096):
                        idx=all_idx[start:start+4096];codes=a[start:start+4096]
                        original=np.array(self.engine.field.anchor[path].reshape(-1,8)[mx.array(idx)])
                        scale=np.array(m.scales.astype(mx.float32)).reshape(-1)[idx]
                        delta=(unpack(codes).astype(np.float32)-unpack(original).astype(np.float32))*scale[:,None]
                        keep=np.max(np.abs(delta),axis=1)>=threshold
                        idx=idx[keep];codes=codes[keep];delta=delta[keep]
                        if not len(idx):continue
                        rows,words=m.weight.shape;gpr=words//8
                        yield dict(module_path=path,tensor_name='weight',logical_shape=[rows,words*8],row_indices=idx//gpr,group_indices=idx%gpr,flat_indices=idx.astype(np.uint32),replacement_packed=codes,signed_delta=delta,checksum=hashlib.sha256(codes.tobytes()).hexdigest(),baseline_ref=baseline_ref,approximate=threshold>0)
            return
        for path,m,*_ in self.engine.field.entries:
            w=m.weight.reshape(-1,8);base=self.engine.field.anchor[path].reshape(-1,8)
            rows,words=m.weight.shape;groups_per_row=words//8
            for start in range(0,w.shape[0],4096):
                p=np.array(w[start:start+4096]);p0=np.array(base[start:start+4096])
                selected=np.flatnonzero(np.any(p!=p0,axis=1))
                if not len(selected):continue
                indices=selected+start
                s=np.array(m.scales.astype(mx.float32)).reshape(-1)[indices]
                delta=(unpack(p[selected]).astype(np.float32)-unpack(p0[selected]).astype(np.float32))*s[:,None]
                keep=np.max(np.abs(delta),axis=1)>=threshold
                indices=indices[keep];delta=delta[keep];codes=p[selected][keep]
                if not len(indices):continue
                yield dict(module_path=path,tensor_name='weight',logical_shape=[rows,words*8],row_indices=(indices//groups_per_row),group_indices=(indices%groups_per_row),flat_indices=indices.astype(np.uint32),replacement_packed=codes,signed_delta=delta,checksum=hashlib.sha256(codes.tobytes()).hexdigest(),baseline_ref=baseline_ref,approximate=threshold>0)

    def export_changes(self,iterator,destination=None,protection_policy=None):
        # Authenticated encryption is mandatory; no private-stream auto capture.
        if protection_policy and protection_policy.get('encrypted') is False:raise ValueError('Change maps require encryption')
        if self.engine.field.pending:raise ValueError('Export requires a committed field boundary')
        entries=[]
        policy=protection_policy or {}
        source_name='map-source-'+uuid.uuid4().hex
        source_meta,source_tensors=self.engine.state(field_only=True)
        source_ref=self.vault.save(source_name,source_meta,source_tensors)
        source_tensors=None
        def tensors():
            for n,entry in enumerate(iterator):
                metadata['approximate']=metadata.get('approximate',False) or entry.get('approximate',False)
                meta={k:v for k,v in entry.items() if not isinstance(v,np.ndarray)}
                meta['part']=n;entries.append(meta)
                for key in ('flat_indices','replacement_packed','signed_delta'):yield str(n)+'.'+key,entry[key]
        name=destination or 'map-'+uuid.uuid4().hex
        metadata=dict(kind='change_map',schema_version=1,baseline_hash=self.engine.manifest['baseline_hash'],tensor_map_version=1,entries=entries,source_run_ref=protection_policy.get('source_run_ref') if protection_policy else None,field_version=self.engine.field.version,token_count=self.engine.field.token_count,config_ref=self.engine.config.to_dict(),rng_identity=self.engine.config.seed)
        metadata.update(map_id=name,baseline_ref=metadata['baseline_hash'],final_field_ref=source_ref,
                        final_checkpoint_hash=source_ref['sha256'],run_id=policy.get('source_run_ref') or source_name,
                        input_stream_ref=policy.get('input_stream_ref'),
                        input_stream_digest=policy.get('input_stream_digest') or hashlib.sha256(json.dumps(self.engine.prefix_hashes).encode()).hexdigest(),
                        input_digest_scope='transcript-message-hashes; raw token streams require caller-supplied digest',
                        source_revision=source_meta['source_revision'],source_versions=source_meta['versions'],
                        config_revision=self.engine.field.config_revision,role_settings=self.engine.config.role_scales,
                        quantization_ref=dict(mode='affine',bits=4,group_size=64),
                        selection_policy=policy.get('selection_policy',{'kind':'iterator_defined; consult entry approximation flags'}),
                        sampling_rng_counter=self.engine.sample_counter)
        try:return self.vault.save(name,metadata,tensors())
        except Exception:
            self.vault.delete(source_name)
            raise

    def materialize_state(self,baseline_ref,change_map_ref,destination=None):
        m=self.vault.manifest(change_map_ref)['metadata']
        if m['baseline_hash']!=baseline_ref or baseline_ref!=self.engine.manifest['baseline_hash']:raise ValueError('Baseline mismatch')
        if self.engine.field.pending:raise ValueError('Materialization requires a committed field boundary')
        if m.get('kind')!='change_map' or m.get('schema_version')!=1 or m.get('tensor_map_version')!=1:raise ValueError('Unsupported change-map format')
        entries={p:module for p,module,*_ in self.engine.field.entries}
        from .change_validation import validate_entry
        seen={}
        arrays=iter(self.vault.arrays(change_map_ref));pending={}
        for entry in m['entries']:
            part=entry['part'];data={}
            for key in ('flat_indices','replacement_packed','signed_delta'):
                name,a=next(arrays)
                if name!=str(part)+'.'+key:raise ValueError('Invalid map order')
                data[key]=a
            p=entry['module_path'];module=entries[p];idx=data['flat_indices'];codes=data['replacement_packed']
            validate_entry(entry,data,module.weight.shape,seen.setdefault(p,set()))
            import mlx.core as mx
            weight=mx.array(pending.get(p,self.engine.field.anchor[p])).reshape(-1,8)
            weight[mx.array(idx)]=mx.array(codes);pending[p]=weight.reshape(module.weight.shape)
        if next(arrays,None) is not None:raise ValueError('Unexpected trailing map tensors')
        mx.eval(pending)
        # Saving a materialized branch never publishes into the active Engine.
        meta,_=self.engine.state(field_only=True)
        meta.update(field_version=m['field_version'],field_tokens=m['token_count'],consumed_tokens=m['token_count'],
                    config=m['config_ref'],changed_words=0,receipts=[],prefix_hashes=[],native_tokens=0,
                    cache=None,last_logits=None,requires_reset=False,source_change_map=change_map_ref,
                    trajectory=dict(tokens=0,buffer=[],descriptors=[],window=self.engine.config.window_tokens,stride=self.engine.config.stride_tokens))
        def tensors():
            for p,module in entries.items():
                if p in pending:
                    weights=pending[p].reshape(-1,8);base=self.engine.field.anchor[p].reshape(-1,8)
                    indices=np.flatnonzero(np.array(mx.any(weights!=base,axis=1))).astype(np.uint32)
                    if len(indices):
                        yield p+'.indices',indices
                        yield p+'.codes',weights[mx.array(indices)]
                yield p+'.frequency',mx.zeros_like(self.engine.field.frequency[p])
        return self.vault.save(destination or 'materialized-'+uuid.uuid4().hex,meta,tensors())

    def import_field(self,checkpoint_ref,new_session=True,reset_native_state=True):
        if not new_session:raise ValueError('Transform trials require an independent Engine/session')
        if self.engine.consumed_tokens or self.engine.field.pending:raise ValueError('Import requires a fresh independent Engine/session')
        self.engine.restore(self.vault,checkpoint_ref,reset_native_state=reset_native_state)
        return dict(baseline_ref=self.engine.manifest['baseline_hash'],field_version=self.engine.field.version,independent=True,native_state_reset=reset_native_state)

