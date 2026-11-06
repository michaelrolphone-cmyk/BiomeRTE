import gc,hashlib,json,time
import numpy as np
import mlx.core as mx
from mlx_lm import load
from mlx.utils import tree_flatten
from mlx_lm.models.cache import ArraysCache,RotatingKVCache
from .adapters.qwen35 import projections,install,install_boundaries,boundary_dimensions
from .field import Field
from . import directions,boundaries
from .trajectory import Trajectory
from .config import FieldConfig
from .manifest import hardware


class Engine:
    def __init__(self,manifest,config=None):
        from experiments.storage import source_identity
        started=time.monotonic();self.manifest=dict(manifest,source_revision='sha256:'+source_identity());self.config=config or FieldConfig()
        self.hardware=hardware()
        self.model,self.tokenizer=load(manifest['path'])
        entries=projections(self.model)
        total=sum(a.nbytes for _,a in tree_flatten(self.model.parameters()))
        mutable=sum(m.weight.nbytes for _,m,*_ in entries)
        cache_estimate=self.config.context_length*32768+64*1024**2
        projection_state_bytes=4*sum(3*m.weight.shape[1]*8+4*m.weight.shape[0] for _,m,*_ in entries)
        operation_state_bytes=0
        for inputs,outputs,contexts in boundary_dimensions(self.model).values():
            operation_state_bytes+=4*(inputs+2*outputs+outputs*max(1,int(outputs**.5))+sum(int(np.prod(shape)) for shape in contexts.values()))
        direction_bytes=projection_state_bytes+operation_state_bytes
        self.memory_estimate=dict(direction_bytes=direction_bytes,model_bytes=total,writable_bytes=mutable,transaction_peak_bytes=total+2*mutable+cache_estimate+2*direction_bytes+512*1024**2,native_cache_estimate=cache_estimate,host_bytes=self.hardware['memory_bytes'])
        if self.memory_estimate['transaction_peak_bytes']>self.hardware['memory_bytes']-3*1024**3:
            raise MemoryError("Insufficient capacity: "+json.dumps(self.memory_estimate))
        mx.set_cache_limit(256*1024**2)
        self.field=Field(entries,self.config)
        install(self.model,self.field.tap)
        install_boundaries(self.model,self.field.boundary_tap)
        self.trajectory=Trajectory(self.config.window_tokens,self.config.stride_tokens,self.config.persistent_homology)
        self.cache=self.make_cache();self.native_tokens=0;self.consumed_tokens=0;self.native_resets=0
        self.sample_counter=0;self.last_logits=None;self.prefix_hashes=[]
        self.input_stream_digest=hashlib.sha256(b'').hexdigest()
        self.input_digest_origin='complete_token_chain'
        self.load_seconds=time.monotonic()-started;self.forward_seconds=0.;self.write_seconds=0.
        self.boundary_hook=None;self.cancel_event=None

    def make_cache(self):
        return [ArraysCache(size=2) if l.is_linear else RotatingKVCache(max_size=self.config.context_length,keep=0) for l in self.model.layers]

    def consume(self,token,role='user',template=False):
        if self.boundary_hook:self.boundary_hook(self)
        if self.cancel_event and self.cancel_event.is_set():raise InterruptedError('cancelled_at_token_boundary')
        start=time.monotonic()
        logits=self.model(mx.array([[token]],mx.int32),cache=self.cache)[0,-1].astype(mx.float32)
        mx.eval(logits)
        if not bool(mx.all(mx.isfinite(logits)).item()):
            raise FloatingPointError("Nonfinite logits; writes stopped")
        self.forward_seconds+=time.monotonic()-start;start=time.monotonic()
        self.field.observe(role,template)
        if self.config.spectral_enabled and not self.config.read_only:self.trajectory.add(self.field.last_regions)
        self.write_seconds+=time.monotonic()-start
        self.native_tokens+=1;self.consumed_tokens+=1;self.last_logits=logits
        # A resumable opaque chain identifies even raw-token experiments without
        # storing token text or relying on message-prefix hashes being present.
        event=json.dumps([int(token),role,bool(template)],separators=(',',':')).encode()
        self.input_stream_digest=hashlib.sha256(bytes.fromhex(self.input_stream_digest)+event).hexdigest()
        return logits

    def ingest_text(self,text,role='user',template=False):
        ids=self.tokenizer.encode(text,add_special_tokens=False)
        for token in ids:self.consume(token,role,template)
        return len(ids)

    def ingest_messages(self,messages):
        """Require exact append-only conversation prefix. Never replay it as writes.

        Render each new role separately; the verified Qwen ChatML template is
        explicit so role/header/special-token writes can be suppressed.
        """
        hashes=[hashlib.sha256(json.dumps(m,sort_keys=True,ensure_ascii=False).encode()).hexdigest() for m in messages]
        if hashes[:len(self.prefix_hashes)]!=self.prefix_hashes:
            raise ValueError("transcript_prefix_mismatch: fork or explicitly reset/replay")
        new=messages[len(self.prefix_hashes):]
        for message in new:
            if set(message)-{'role','content'} or message['role'] not in self.config.role_scales or not isinstance(message['content'],str):
                raise ValueError("Text messages require role and content only")
            role=message['role']
            self.ingest_text('<|im_start|>'+role+'\n',role,True)
            self.ingest_text(message['content'],role)
            self.ingest_text('<|im_end|>\n',role,True)
        self.prefix_hashes=hashes
        self.ingest_text('<|im_start|>assistant\n<think>\n\n</think>\n\n','assistant',True)

    def generate(self,messages,max_tokens=128,temperature=0.,cancel=None,emit=None):
        if getattr(self,'requires_reset',False):raise ValueError('Cancelled prefill requires ordinary-state reset before continuing')
        try:return self._generate(messages,max_tokens,temperature,cancel,emit)
        except InterruptedError:
            self.cancel_event=None;self.field.commit();self.requires_reset=True
            return dict(text=getattr(self,'partial_text',''),finish_reason='cancelled',requires_ordinary_reset=True,diagnostics=self.diagnostics())
        finally:self.cancel_event=None

    def _generate(self,messages,max_tokens=128,temperature=0.,cancel=None,emit=None):
        if type(max_tokens) is not int or max_tokens<1:raise ValueError("Positive max_tokens required")
        if not np.isfinite(temperature) or temperature<0:raise ValueError("Invalid temperature")
        self.cancel_event=cancel;self.partial_text=''
        self.ingest_messages(messages)
        generated=[];text='';reason='length'
        for _ in range(max_tokens):
            if cancel and cancel.is_set():reason='cancelled';break
            logits=self.last_logits
            if temperature==0:token=int(mx.argmax(logits).item())
            else:
                key=mx.random.key((self.config.seed+self.sample_counter)&0xffffffff)
                token=int(mx.random.categorical(logits/temperature,key=key).item())
                self.sample_counter+=1
            eos=token in self.tokenizer.eos_token_ids
            # Final sampled token is consumed exactly once, even at stop/length.
            self.consume(token,'assistant',template=eos)
            if eos:reason='stop';break
            generated.append(token);full=self.tokenizer.decode(generated)
            if full.endswith('\ufffd'):continue
            if not full.startswith(text):raise RuntimeError("Tokenizer stream prefix changed")
            chunk=full[len(text):];text=full;self.partial_text=text
            if emit:emit(dict(type='token',text=chunk,committed_token_count=self.consumed_tokens,field_version=self.field.version))
        self.field.commit();self.cancel_event=None
        # Seal the assistant turn for subsequent new messages. Don't doublefeed
        # the generated body. Header/footer are explicit non-writing tokens.
        if reason!='stop':self.ingest_text('<|im_end|>','assistant',True)
        self.ingest_text('\n','assistant',True)
        self.prefix_hashes.append(hashlib.sha256(json.dumps(dict(role='assistant',content=text),sort_keys=True,ensure_ascii=False).encode()).hexdigest())
        return dict(text=text,finish_reason=reason,model=self.manifest['repository'],model_revision=self.manifest['revision'],diagnostics=self.diagnostics())

    def reset(self,kind,retain_traces=False):
        if kind not in ['ordinary_state','activity_traces','weights','entire_session']:raise ValueError("Unknown reset kind")
        if kind=='ordinary_state':self.field.commit()
        if kind in ['ordinary_state','weights','entire_session']:
            self.requires_reset=False
            self.cache=self.make_cache();self.native_tokens=0;self.native_resets+=1;self.last_logits=None;self.prefix_hashes=[]
        self.field.reset(kind,retain_traces)
        if kind=='entire_session':
            self.input_stream_digest=hashlib.sha256(b'').hexdigest();self.input_digest_origin='complete_token_chain'
            self.sample_counter=0;self.consumed_tokens=0
        if kind in ['activity_traces','entire_session'] or (kind=='weights' and not retain_traces):
            self.trajectory.close();self.trajectory=Trajectory(self.config.window_tokens,self.config.stride_tokens,self.config.persistent_homology)

    def patch_config(self,patch):
        merged=self.config.to_dict();merged.update(patch);config=FieldConfig.parse(merged)
        if config.context_length!=self.config.context_length and self.native_tokens:
            raise ValueError("Reset ordinary state before changing context length")
        self.field.commit()
        if (config.window_tokens,config.stride_tokens,config.persistent_homology)!=(self.config.window_tokens,self.config.stride_tokens,self.config.persistent_homology):
            self.trajectory.close();self.trajectory=Trajectory(config.window_tokens,config.stride_tokens,config.persistent_homology)
        self.config=config;self.field.config=config;self.field.config_revision+=1

    def state(self,field_only=False):
        self.field.commit();tensors=[]
        for p,m,*_ in self.field.entries:
            groups=m.weight.reshape(-1,8);original=self.field.anchor[p].reshape(-1,8)
            mask=np.array(mx.any(groups!=original,axis=1))
            indices=np.flatnonzero(mask).astype(np.uint32)
            if len(indices):
                tensors.extend([(p+'.indices',indices),(p+'.codes',groups[mx.array(indices)])])
            tensors.append((p+'.frequency',self.field.frequency[p]))
        tensors.extend(directions.tensors(self.field.directions))
        tensors.extend(boundaries.tensors(self.field.boundaries))
        def encode(value,path):
            if isinstance(value,mx.array):
                dtype=str(value.dtype).split('.')[-1]
                # Store BF16 bits exactly; never silently cast cache dtypes.
                tensors.append((path,value.view(mx.uint16) if value.dtype==mx.bfloat16 else value))
                return {'tensor':path,'mlx_dtype':dtype}
            if isinstance(value,(tuple,list)):return [encode(v,path+'.'+str(i)) for i,v in enumerate(value)]
            if value is None or isinstance(value,(int,float,str,bool)):return value
            raise ValueError("Unsupported cache state")
        metadata=dict(baseline_hash=self.manifest['baseline_hash'],tensor_map_version=1,kind='field_only' if field_only else 'complete',config=self.config.to_dict(),field_version=self.field.version,field_tokens=self.field.token_count,consumed_tokens=self.consumed_tokens,sample_counter=self.sample_counter,native_tokens=0 if field_only else self.native_tokens,config_revision=self.field.config_revision,changed_words=self.field.changed_words,receipts=self.field.receipts,trajectory=self.trajectory.state(),prefix_hashes=[] if field_only else self.prefix_hashes,cache=None if field_only else [encode(c.state,'cache.'+str(i)) for i,c in enumerate(self.cache)],last_logits=None if field_only or self.last_logits is None else encode(self.last_logits,'last_logits'),versions=self.manifest['versions'],source_revision=self.manifest.get('source_revision','uncommitted'))
        metadata['boundaries']=boundaries.metadata(self.field.boundaries,self.field.version)
        metadata['directions']=directions.metadata(self.field.directions,self.field.version)
        metadata['requires_reset']=getattr(self,'requires_reset',False)
        metadata['input_stream_digest']=self.input_stream_digest
        metadata['input_digest_origin']=self.input_digest_origin
        return metadata,tensors

    def restore(self,vault,name,reset_native_state=False):
        manifest=vault.manifest(name);meta=manifest['metadata']
        if meta['baseline_hash']!=self.manifest['baseline_hash'] or meta['tensor_map_version']!=1:raise ValueError("Baseline or tensor map mismatch")
        entries={p:m for p,m,*_ in self.field.entries};indices={};proposed={};other={}
        for key,a in vault.arrays(name):
            if key.endswith('.indices'):indices[key[:-8]]=a
            elif key.endswith('.codes'):
                p=key[:-6]
                if p not in entries or p not in indices or a.dtype!=np.uint32 or a.shape!=(len(indices[p]),8):raise ValueError("Invalid packed change data")
                m=entries[p];base=self.field.anchor[p].reshape(-1,8)
                idx=indices.pop(p)
                if idx.dtype!=np.uint32 or idx.ndim!=1 or (len(idx) and (np.max(idx)>=base.shape[0] or len(np.unique(idx))!=len(idx))):raise ValueError("Invalid group index")
                # MLX arrays have value semantics; assignment creates new storage.
                changed=mx.array(base);changed[mx.array(idx)]=mx.array(a)
                proposed[p]=changed.reshape(m.weight.shape);mx.eval(proposed[p])
            elif key.endswith('.frequency'):
                p=key[:-10]
                if p not in entries or a.shape!=(entries[p].weight.shape[0],) or not np.isfinite(a).all():raise ValueError("Invalid frequency state")
                other[key]=mx.array(a)
            else:other[key]=mx.array(a)
        if indices:raise ValueError("Incomplete changes")
        def decode(value):
            if isinstance(value,dict) and 'tensor' in value:
                a=other[value['tensor']]
                return a.view(mx.bfloat16) if value['mlx_dtype']=='bfloat16' else a
            if isinstance(value,list):return [decode(v) for v in value]
            return value
        restored_config=FieldConfig.parse(meta['config'])
        frequencies={p:other[p+'.frequency'] for p in entries}
        restored_logits=None if reset_native_state else decode(meta['last_logits'])
        trajectory=meta['trajectory']
        if not isinstance(trajectory.get('tokens'),int) or not isinstance(trajectory.get('buffer'),list) or not isinstance(trajectory.get('descriptors'),list):
            raise ValueError('Invalid trajectory state')
        counters={key:meta[key] for key in ('field_version','field_tokens','config_revision','changed_words','consumed_tokens','sample_counter','native_tokens')}
        if any(type(v) is not int or v<0 for v in counters.values()):raise ValueError('Invalid checkpoint counter')
        if not isinstance(meta['receipts'],list) or not isinstance(meta['prefix_hashes'],list) or any(not isinstance(h,str) or len(h)!=64 for h in meta['prefix_hashes']):
            raise ValueError('Invalid checkpoint receipts or transcript identity')
        if type(meta.get('requires_reset',False)) is not bool:raise ValueError('Invalid reset flag')
        stream_digest=meta.get('input_stream_digest')
        digest_origin=meta.get('input_digest_origin','complete_token_chain')
        if stream_digest is None:
            # Older checkpoints remain readable, but their missing prefix is
            # explicitly represented rather than invented as a known stream.
            stream_digest=hashlib.sha256(('legacy-prefix:'+name).encode()).hexdigest()
            digest_origin='legacy_checkpoint_prefix_unknown:'+name
        if not isinstance(digest_origin,str):raise ValueError('Invalid input digest origin')
        if not isinstance(stream_digest,str) or len(stream_digest)!=64:
            raise ValueError('Invalid input stream digest')
        try:bytes.fromhex(stream_digest)
        except ValueError as exc:raise ValueError('Invalid input stream digest') from exc
        if meta['kind'] not in ('complete','field_only'):raise ValueError('Invalid checkpoint kind')
        if meta['kind']=='complete' and meta['native_tokens'] and meta['cache'] is None:raise ValueError('Complete checkpoint missing native state')
        newcache=[ArraysCache(size=2) if l.is_linear else RotatingKVCache(max_size=restored_config.context_length,keep=0) for l in self.model.layers]
        if not reset_native_state and meta['cache'] is not None:
            for c,v in zip(newcache,meta['cache'],strict=True):c.state=decode(v)
        direction_state=directions.restore(meta.get('directions'),
            {k:v for k,v in other.items() if k.startswith(directions.PREFIX)},
            {p:(m.weight.shape[1]*8,m.weight.shape[0]) for p,m in entries.items()},
            meta['field_version'],xp=mx)
        boundary_state=boundaries.restore(meta.get('boundaries'),
            {k:v for k,v in other.items() if k.startswith(boundaries.PREFIX)},
            meta['field_version'],boundary_dimensions(self.model),xp=mx)
        # Publish only after every encrypted shard and state tree validates.
        newtrajectory=Trajectory(restored_config.window_tokens,restored_config.stride_tokens,restored_config.persistent_homology)
        newtrajectory.restore(trajectory)
        for p,m in entries.items():m.weight=proposed.get(p,self.field.anchor[p]);self.field.frequency[p]=frequencies[p]
        self.config=restored_config;self.field.config=restored_config
        self.field.pending.clear();self.field.pending_boundaries=None;self.field.captured.clear()
        self.field.version=meta['field_version'];self.field.token_count=meta['field_tokens'];self.field.config_revision=meta['config_revision'];self.field.changed_words=meta['changed_words'];self.field.receipts=meta['receipts']
        self.field.norm_cache.clear()
        self.field.directions=direction_state
        self.field.boundaries=boundary_state
        self.field.boundary_captured.clear()
        self.cache=newcache;self.native_tokens=0 if reset_native_state else meta['native_tokens'];self.consumed_tokens=meta['consumed_tokens'];self.sample_counter=meta['sample_counter'];self.prefix_hashes=[] if reset_native_state else meta['prefix_hashes']
        self.last_logits=restored_logits
        self.input_stream_digest=stream_digest
        self.input_digest_origin=digest_origin
        oldtrajectory=self.trajectory;self.trajectory=newtrajectory;oldtrajectory.close()
        self.requires_reset=False if reset_native_state else meta.get('requires_reset',False)

    def diagnostics(self):
        return dict(**self.field.diagnostics(),native_tokens=self.native_tokens,consumed_token_count=self.consumed_tokens,ordinary_resets=self.native_resets,spectral=self.trajectory.diagnostics(),memory=self.memory_estimate,mlx_peak_memory=mx.get_peak_memory(),mlx_active_memory=mx.get_active_memory(),load_seconds=self.load_seconds,forward_seconds=self.forward_seconds,write_seconds=self.write_seconds,baseline_hash=self.manifest['baseline_hash'])

    def close(self):
        self.trajectory.close();self.field.pending.clear();self.field.pending_boundaries=None;self.field.captured.clear();self.field.boundary_captured.clear()
        self.model=None;self.cache=None;self.last_logits=None;self.field=None
        gc.collect();mx.clear_cache()


