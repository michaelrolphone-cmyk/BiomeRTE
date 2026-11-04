"""Streaming extension seam for deferred domain-transform experiments."""
import hashlib,uuid
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
        entries=[]
        def tensors():
            for n,entry in enumerate(iterator):
                meta={k:v for k,v in entry.items() if not isinstance(v,np.ndarray)}
                meta['part']=n;entries.append(meta)
                for key in ('flat_indices','replacement_packed','signed_delta'):yield str(n)+'.'+key,entry[key]
        name=destination or 'map-'+uuid.uuid4().hex
        metadata=dict(kind='change_map',schema_version=1,baseline_hash=self.engine.manifest['baseline_hash'],tensor_map_version=1,entries=entries,source_run_ref=protection_policy.get('source_run_ref') if protection_policy else None,field_version=self.engine.field.version,token_count=self.engine.field.token_count,config_ref=self.engine.config.to_dict(),rng_identity=self.engine.config.seed)
        return self.vault.save(name,metadata,tensors())

    def materialize_state(self,baseline_ref,change_map_ref,destination=None):
        m=self.vault.manifest(change_map_ref)['metadata']
        if m['baseline_hash']!=baseline_ref or baseline_ref!=self.engine.manifest['baseline_hash']:raise ValueError('Baseline mismatch')
        self.engine.reset('entire_session');entries={p:module for p,module,*_ in self.engine.field.entries}
        arrays=iter(self.vault.arrays(change_map_ref));pending={}
        for entry in m['entries']:
            part=entry['part'];data={}
            for key in ('flat_indices','replacement_packed','signed_delta'):
                name,a=next(arrays)
                if name!=str(part)+'.'+key:raise ValueError('Invalid map order')
                data[key]=a
            p=entry['module_path'];module=entries[p];idx=data['flat_indices'];codes=data['replacement_packed']
            if codes.dtype!=np.uint32 or codes.shape!=(len(idx),8) or np.any(idx>=module.weight.size//8):raise ValueError('Invalid replacement groups')
            import mlx.core as mx
            weight=pending.get(p,module.weight).reshape(-1,8)
            weight[mx.array(idx)]=mx.array(codes);pending[p]=weight.reshape(module.weight.shape)
        for p,weight in pending.items():entries[p].weight=weight
        meta,tensors=self.engine.state(field_only=True)
        return self.vault.save(destination or 'materialized-'+uuid.uuid4().hex,meta,tensors)

    def import_field(self,checkpoint_ref,new_session=True,reset_native_state=True):
        if not new_session:raise ValueError('Transform trials require an independent Engine/session')
        self.engine.reset('entire_session');self.engine.restore(self.vault,checkpoint_ref,reset_native_state=reset_native_state)
        return dict(baseline_ref=self.engine.manifest['baseline_hash'],field_version=self.engine.field.version,independent=True,native_state_reset=reset_native_state)
