"""Approximate reverse propagation, ending at the emitted token sequence."""
import hashlib
import tempfile
from pathlib import Path
import numpy as np
from .checkpoint import canonical
from .inverse import InverseOptions
from .reverse import ReverseUnavailable, emit_tokens
from . import directions, boundaries
from .context_reconstruction.snapshot import field_arrays


def require_direction_state(metadata):
    state=metadata.get('directions')
    if not isinstance(state,dict) or not state.get('projections'):
        raise ReverseUnavailable('direction_state_missing',
            'This checkpoint has no accumulated propagation directions. '
            'They cannot be manufactured from its weight deltas; no dictionary fallback is used.')
    if state.get('schema_version')!=1 or state.get('coverage')!='quantized_projections':
        raise ReverseUnavailable('direction_state_unsupported','Unsupported propagation direction state')
    operations=metadata.get('boundaries')
    if not isinstance(operations,dict) or not operations.get('operations'):
        raise ReverseUnavailable('operation_state_missing','This checkpoint has no full-model operation boundary state')
    return state


def load_present_state(engine,vault,checkpoint,metadata,check=lambda:None):
    import mlx.core as mx
    from .adapters.qwen35 import boundary_dimensions
    entries={p:m for p,m,*_ in engine.field.entries}
    pending={};proposed={};direction_arrays={};boundary_arrays={};seen=set()
    for key,array in field_arrays(vault,checkpoint):
        check()
        if key in seen:raise ValueError('Duplicate present-state tensor')
        seen.add(key)
        if key.startswith(directions.PREFIX):direction_arrays[key]=array
        elif key.startswith(boundaries.PREFIX):boundary_arrays[key]=array
        elif key.endswith('.indices'):
            path=key[:-8]
            if path not in entries or array.dtype!=np.uint32 or array.ndim!=1:
                raise ValueError('Invalid source projection indices')
            pending[path]=array
        elif key.endswith('.codes'):
            path=key[:-6];indices=pending.pop(path,None)
            if path not in entries or indices is None or array.dtype!=np.uint32 or array.shape!=(len(indices),8):
                raise ValueError('Invalid source packed codes')
            module=entries[path];base=engine.field.anchor[path].reshape(-1,8)
            if len(indices) and (int(indices.max())>=base.shape[0] or len(np.unique(indices))!=len(indices)):
                raise ValueError('Invalid source group coordinates')
            current=mx.array(base);current[mx.array(indices)]=mx.array(array)
            proposed[path]=current.reshape(module.weight.shape);mx.eval(proposed[path])
    if pending:raise ValueError('Incomplete packed changes')
    version=metadata['directions']['field_version']
    projected=directions.restore(metadata['directions'],direction_arrays,
        {p:(m.weight.shape[1]*8,m.weight.shape[0]) for p,m in entries.items()},version,xp=mx)
    operations=boundaries.restore(metadata['boundaries'],boundary_arrays,version,
                                  boundary_dimensions(engine.model))
    for path,module in entries.items():module.weight=proposed.get(path,engine.field.anchor[path])
    engine.field.directions=projected
    engine.field.boundaries=operations
    engine.field.capture=False
    return operations


def token_readout(engine,cache_root,check=lambda:None,progress=lambda event:None):
    """Build the frozen embedding index once, then reuse branch-only readout."""
    import mlx.core as mx
    from .token_readout import PartitionTokenReadout
    fingerprint=engine.manifest['baseline_hash']
    root=Path(cache_root);root.mkdir(parents=True,exist_ok=True)
    target=root/(fingerprint+'.npz')
    if target.exists():return PartitionTokenReadout.load(target,fingerprint)
    embedding=engine.model.model.embed_tokens
    count=embedding.weight.shape[0]
    special=set(engine.tokenizer.all_special_ids)
    ids=np.array([i for i in range(count) if i not in special],dtype=np.int64)
    if not len(ids):raise ValueError('Model has no non-special embedding tokens')
    dimension=int(embedding(mx.array([int(ids[0])])).shape[-1])
    # This is a one-time codebook construction, never a per-emission token scan.
    # The temporary holds only public/frozen baseline embeddings, not field state.
    with tempfile.TemporaryDirectory(prefix='embedding-index-',dir=root) as folder:
        matrix=np.memmap(Path(folder)/'embeddings.f32',dtype=np.float32,mode='w+',shape=(len(ids),dimension))
        for start in range(0,len(ids),1024):
            check()
            batch=np.array(embedding(mx.array(ids[start:start+1024])).astype(mx.float32))
            batch/=np.maximum(np.linalg.norm(batch,axis=1,keepdims=True),1e-12)
            matrix[start:start+len(batch)]=batch
            progress(dict(phase='embedding_index',completed=min(start+len(batch),len(ids)),total=len(ids)))
        matrix.flush()
        readout=PartitionTokenReadout.from_embeddings(matrix,token_ids=ids,normalize=False,check=check)
        del matrix
    check()
    readout.save(target,fingerprint)
    return readout


def reconstruct(manifest,vault,checkpoint,options=None,progress=lambda event:None,
                check=lambda:None,readout_cache=None):
    InverseOptions.parse(options)
    check()
    source=vault.manifest(checkpoint);metadata=source['metadata']
    if (metadata.get('baseline_hash')!=manifest['baseline_hash'] or
            metadata.get('tensor_map_version')!=1 or
            metadata.get('kind') not in ('complete','field_only')):
        raise ValueError('Baseline, tensor map or checkpoint kind mismatch')
    require_direction_state(metadata)
    from .runtime import Engine
    from .config import FieldConfig
    from .admission import model_slot
    from .adapters.qwen35_reverse import Qwen35ReverseAdapter
    config=FieldConfig.parse(dict(metadata['config'],enabled=False,read_only=True,spectral_enabled=False))
    with model_slot(lambda event:progress(dict(phase='admission',**event))):
        engine=Engine(manifest,config)
        try:
            records=load_present_state(engine,vault,checkpoint,metadata,check)
            readout=token_readout(engine,readout_cache or (vault.root.parent/'token-readout'),check,progress)
            adapter=Qwen35ReverseAdapter(engine,records,readout)
            tokens=[]
            for token in emit_tokens(adapter,records,records,engine.field.entries,check):
                tokens.append(token)
                progress(dict(phase='reverse_propagation',emitted_tokens=len(tokens)))
            # No replay, activation comparison, quality gate or subsequent model stage.
            return dict(status='complete',method='approximate_reverse_propagation',
                estimated_token_ids=tokens,context_text=engine.tokenizer.decode(tokens),
                token_order='reverse_trajectory',source_modified=False,
                source_refs=[dict(checkpoint_id=checkpoint,sha256=hashlib.sha256(canonical(source)).hexdigest(),
                                  baseline_hash=manifest['baseline_hash'])],
                uses_source_transcript=False,uses_input_token_history=False,uses_source_logits=False,
                uses_recurrent_present_state=True,uses_attention_aggregate=True,
                readout='frozen_embedding_partition',termination='accumulated_motion_or_zero_direction',
                approximation='local operation adjoints at aggregate state; semantic quality unmeasured')
        finally:
            engine.close()


def reconstruct_with_adapter(adapter,present_state,directions,weights,decode,check=lambda:None):
    """Generic operation adapter integration, also used by mechanical tests."""
    tokens=list(emit_tokens(adapter,present_state,directions,weights,check))
    return dict(status='complete',method='approximate_reverse_propagation',
                estimated_token_ids=tokens,context_text=decode(tokens),
                token_order='reverse_trajectory',source_modified=False)
