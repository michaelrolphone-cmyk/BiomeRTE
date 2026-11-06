"""Inverse experiment on the real pinned model; source checkpoints stay read-only."""
import hashlib, heapq, time
import numpy as np
import mlx.core as mx
from .checkpoint import canonical
from .context_reconstruction.snapshot import weight_arrays
from .config import FieldConfig
from .inverse import InverseOptions, delta_basis, beam_reconstruct, first_pass_result
from .quantized_writer import unpack


class DeltaTarget:
    def __init__(self, engine, vault, checkpoint, check=lambda:None):
        self.check=check
        self.engine=engine;self.entries={p:m for p,m,*_ in engine.field.entries}
        manifest=vault.manifest(checkpoint);self.metadata=manifest['metadata']
        if self.metadata.get('baseline_hash')!=engine.manifest['baseline_hash'] or self.metadata.get('tensor_map_version')!=1:
            raise ValueError('Baseline or tensor-map mismatch')
        if self.metadata.get('kind') not in ('complete','field_only'):
            raise ValueError('Reconstruction requires a model checkpoint')
        self.source_hash=hashlib.sha256(canonical(manifest)).hexdigest()
        self.groups={};pending={}
        for key,array in weight_arrays(vault,checkpoint):
            check()
            if key.endswith('.indices'):
                path=key[:-8]
                if path not in self.entries or path in pending or path in self.groups:
                    raise ValueError('Unknown or duplicate source projection')
                if array.dtype!=np.uint32 or array.ndim!=1:raise ValueError('Invalid source indices')
                pending[path]=array
            elif key.endswith('.codes'):
                path=key[:-6];idx=pending.pop(path,None)
                if idx is None or array.dtype!=np.uint32 or array.shape!=(len(idx),8):
                    raise ValueError('Invalid source packed codes')
                if len(idx) and (int(idx.max())>=self.entries[path].weight.size//8 or len(np.unique(idx))!=len(idx)):
                    raise ValueError('Source group coordinates invalid')
                order=np.argsort(idx);self.groups[path]=(idx[order],array[order])
            # Deliberately ignore native caches, prefix hashes, logits and input digests.
        if pending:raise ValueError('Incomplete source packed change set')
        self.energy=0.;self.changed_groups=sum(len(x[0]) for x in self.groups.values())
        for path,(idx,codes) in self.groups.items():
            for start in range(0,len(idx),4096):
                self.check()
                ids=idx[start:start+4096]
                delta=self.numeric_delta(path,ids,codes[start:start+4096])
                self.energy+=float(np.sum(delta*delta,dtype=np.float64))

    def numeric_delta(self,path,idx,codes):
        module=self.entries[path];base=self.engine.field.anchor[path].reshape(-1,8)
        original=np.array(base[mx.array(idx)])
        scales=np.array(module.scales.astype(mx.float32)).reshape(-1)[idx]
        return (unpack(codes).astype(np.float64)-unpack(original).astype(np.float64))*scales[:,None]

    def proposal_blocks(self):
        paths=[];blocks=[];dimension=None
        for path,(idx,codes) in self.groups.items():
            if not path.startswith('language_model.model.layers.0.linear_attn.in_proj_'):continue
            module=self.entries[path];dim=module.weight.shape[1]*8
            if dimension is not None and dimension!=dim:raise ValueError('Proposal input dimensions disagree')
            dimension=dim;gpr=dim//64
            blocks.append((idx//gpr,idx%gpr,self.numeric_delta(path,idx,codes),module.weight.shape[0]));paths.append(path)
        return blocks,dimension,paths

    def score(self):
        error=0.;candidate_energy=0.;covered=0
        for path,module in self.entries.items():
            self.check()
            current=module.weight.reshape(-1,8);base=self.engine.field.anchor[path].reshape(-1,8)
            changed=np.flatnonzero(np.array(mx.any(current!=base,axis=1))).astype(np.uint32)
            idx,target=self.groups.get(path,(np.empty(0,np.uint32),np.empty((0,8),np.uint32)))
            union=np.union1d(idx,changed).astype(np.uint32)
            for start in range(0,len(union),4096):
                self.check()
                ids=union[start:start+4096];actual=self.numeric_delta(path,ids,np.array(current[mx.array(ids)]))
                expected=np.zeros_like(actual)
                positions=np.searchsorted(idx,ids);matched=positions<len(idx)
                if len(idx):matched[matched]&=idx[positions[matched]]==ids[matched]
                if matched.any():expected[matched]=self.numeric_delta(path,ids[matched],target[positions[matched]])
                difference=actual-expected
                error+=float(np.sum(difference*difference,dtype=np.float64))
                candidate_energy+=float(np.sum(actual*actual,dtype=np.float64));covered+=len(ids)
        return dict(relative_squared_error=error/self.energy if self.energy else (0. if error==0 else error),
                    squared_error=error,target_squared_norm=self.energy,candidate_squared_norm=candidate_energy,
                    compared_changed_union_groups=covered,scored_projections=len(self.entries),scored_layers=len(self.engine.model.layers))


def propose_tokens(engine,target,basis,options,progress=lambda event:None):
    model=engine.model;embedding=model.model.embed_tokens;norm=model.layers[0].input_layernorm
    vocab=embedding.weight.shape[0];special=set(engine.tokenizer.all_special_ids)
    directions=mx.array(basis);heap=[]
    # A changed row is direct evidence that its update gate fired. Quantization
    # can erase a small update, so absence of change is not a negative label.
    signatures=[]
    for path,(indices,codes) in target.groups.items():
        if not path.startswith('language_model.model.layers.0.linear_attn.in_proj_'):continue
        module=target.entries[path];gpr=module.weight.shape[1]//8
        rows=np.unique(indices//gpr)
        mask=np.zeros(module.weight.shape[0],np.float32);mask[rows]=1.
        signatures.append((module,mx.array(mask)))
    for start in range(0,vocab,options.vocabulary_batch):
        progress(dict(phase='vocabulary',completed=start,total=vocab))
        stop=min(vocab,start+options.vocabulary_batch);ids=mx.arange(start,stop)
        inputs=norm(embedding(ids));matches=mx.zeros((stop-start,),mx.float32)
        observed=0
        for module,mask in signatures:
            y=module(inputs).astype(mx.float32)
            z=mx.clip(y/mx.sqrt(mx.mean(y*y,axis=-1,keepdims=True)+1e-12),-4,4)
            fired=(mx.abs(z)>=engine.config.amplitude_threshold).astype(mx.float32)
            # Coverage rewards a token whose predicted gates explain observed
            # writes. Precision penalizes extraneous predicted writing rows.
            overlap=mx.sum(fired*mask,axis=-1)
            matches+=2*overlap/(mx.sum(fired,axis=-1)+mx.sum(mask)+1e-6)
            observed+=1
        x=inputs.astype(mx.float32)
        x=mx.clip(x/mx.sqrt(mx.mean(x*x,axis=-1,keepdims=True)+1e-12),-4,4)
        x=x/mx.maximum(mx.linalg.norm(x,axis=-1,keepdims=True),1e-6)
        scores=np.array(matches/max(1,observed)+0.001*mx.sum((x@directions)**2,axis=-1))
        for token,score in zip(range(start,stop),scores):
            if token in special:continue
            item=(float(score),-token)
            if len(heap)<options.candidate_pool:heapq.heappush(heap,item)
            elif item>heap[0]:heapq.heapreplace(heap,item)
        progress(dict(phase='vocabulary',completed=stop,total=vocab))
    return [dict(token_id=-token,proposal_score=score) for score,token in sorted(heap,reverse=True)]


def reconstruct(manifest,vault,checkpoint,options=None,progress=lambda event:None,check=lambda:None,memo=None,save_score=lambda key,value:None):
    from .runtime import Engine
    from .admission import model_slot
    from experiments.storage import source_identity
    options=InverseOptions.parse(options);started=time.monotonic();memo={} if memo is None else memo
    check()
    source_manifest=vault.manifest(checkpoint)
    source_config=FieldConfig.parse(source_manifest['metadata']['config'])
    # Proposals inspect a fresh baseline; replay writes only to that private Engine.
    config=source_config.to_dict();config.update(enabled=False,read_only=False,spectral_enabled=False)
    with model_slot(lambda event:progress(dict(phase='admission',**event))):
        engine=Engine(manifest,FieldConfig.parse(config));target=None
        try:
            engine.boundary_hook=lambda _:check()
            target=DeltaTarget(engine,vault,checkpoint,check)
            source_ref=dict(checkpoint_id=checkpoint,sha256=target.source_hash,baseline_hash=manifest['baseline_hash'])
            common=dict(schema_version=1,source_refs=[source_ref],method=('delta_gate_signature_vocabulary_first_pass' if options.mode=='first_pass'
                                else 'delta_gate_signature_vocabulary_then_full_model_replay'),
                        options=options.to_dict(),source_revision=source_identity(),uses_source_transcript=False,
                        uses_source_native_caches=False,uses_source_logits=False,source_modified=False,
                        source_changed_groups=target.changed_groups,target_squared_norm=target.energy)
            blocks,dimension,paths=target.proposal_blocks()
            if not dimension or not target.energy:
                return dict(**common,status='insufficient_signal',context_text='',candidates=[],
                            uncertainty_and_gaps=['No usable first-layer input-projection delta; no context inferred.'])
            progress(dict(phase='delta_subspace',projection_count=len(paths)))
            basis,diagnostics=delta_basis(blocks,dimension,options.sketch_rank,options.power_iterations,options.seed)
            blocks=None
            if not basis.shape[1]:raise ValueError('No nonzero delta directions')
            proposals=propose_tokens(engine,target,basis,options,progress);basis=None
            if options.mode=='first_pass':
                check()
                if hashlib.sha256(canonical(vault.manifest(checkpoint))).hexdigest()!=target.source_hash:
                    raise ValueError('Source checkpoint changed during reconstruction')
                result=first_pass_result(proposals,engine.tokenizer.decode)
                return dict(**common,**result,proposal_projections=paths,
                            proposal_diagnostics=diagnostics,seconds=time.monotonic()-started)
            replay_config=source_config.to_dict();replay_config.update(enabled=True,read_only=False,spectral_enabled=False)
            engine.patch_config(replay_config)
            def score(ids):
                check();key=','.join(map(str,ids))
                if key in memo:return memo[key]
                engine.reset('entire_session')
                if options.layout=='message':engine.ingest_text('<|im_start|>'+options.role+'\n',options.role,True)
                for token in ids:engine.consume(token,options.role)
                if options.layout=='message':engine.ingest_text('<|im_end|>\n',options.role,True)
                engine.field.commit()
                measurement=target.score();save_score(key,measurement);memo[key]=measurement
                return measurement
            candidates=beam_reconstruct([p['token_id'] for p in proposals],score,options,progress)
            for item in candidates:item['text']=engine.tokenizer.decode(item['token_ids'])
            if hashlib.sha256(canonical(vault.manifest(checkpoint))).hexdigest()!=target.source_hash:
                raise ValueError('Source checkpoint changed during reconstruction')
            best=candidates[0];gaps=[
                'Candidate context inferred from weight differences; original wording and order are unverified.',
                'Aggregate updates can have several matching input sequences, including sign and order ambiguities.',
                'Search covers the reported delta-derived token pool and requested token depth; it is not exhaustive.',
                'Replay assumes fresh native caches, zero initial activity traces and the recorded final write configuration.',
                'Mixed roles, earlier configuration changes, resets or omitted template history can prevent a close replay match.']
            return dict(**common,status='complete',context_text=best['text'],candidate_kind='hypothesis',
                        candidates=candidates,token_proposals=proposals,proposal_projections=paths,
                        proposal_diagnostics=diagnostics,replay_evaluations=len(candidates),
                        best_relative_squared_error=best['relative_squared_error'],uncertainty_and_gaps=gaps,
                        scientific_reconstruction_success='not_established',seconds=time.monotonic()-started)
        finally:
            target=None
            engine.close()
