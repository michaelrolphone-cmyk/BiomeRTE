import hashlib,time
import numpy as np
import mlx.core as mx
from .kernels.field_write import row_norm,write,audit
from .quantized_writer import reference_write,write_coefficients
from .directions import accumulate
from . import boundaries


class Field:
    def __init__(self, entries, config):
        self.entries=entries
        self.config=config
        self.anchor={p:m.weight for p,m,*_ in entries}
        self.frequency={p:mx.zeros((m.weight.shape[0],),mx.float32) for p,m,*_ in entries}
        self.pending=[]
        self.captured={}
        self.token_count=0
        self.version=0
        self.receipts=[]
        self.last_regions=None
        self.changed_words=0
        self.norm_cache={}
        self.config_revision=0
        self.capture=True
        self.directions={}
        self.boundaries={}
        self.boundary_captured={}
        self.pending_boundaries=None

    def tap(self,path,x,y):
        if not self.capture: return
        if x.shape[:-1]!=(1,1) or y.shape[:-1]!=(1,1):
            raise ValueError("Online field requires exactly one token")
        x=x.reshape(-1).astype(mx.float32);y=y.reshape(-1).astype(mx.float32)
        xhat=mx.clip(x/mx.sqrt(mx.mean(x*x)+1e-12),-4,4)
        xunit=xhat/mx.maximum(mx.linalg.norm(xhat),1e-6)
        z=mx.clip(y/mx.sqrt(mx.mean(y*y)+1e-12),-4,4)
        self.captured[path]=(xunit,z)

    def boundary_tap(self,path,x,y,context):
        if not self.capture:return
        if x.shape[:-1]!=(1,1) or y.shape[:-1]!=(1,1):
            raise ValueError("Online boundaries require exactly one token")
        self.boundary_captured[path]=(x.astype(mx.float32),y.astype(mx.float32),context)

    def observe(self,role,template=False):
        c=self.config
        if c.read_only:
            self.captured.clear();self.boundary_captured.clear();return
        beta=2**(-1/c.frequency_half_life_tokens)
        pending={}; sums=[mx.zeros((8,),mx.float32) for _ in range(64)]; counts=np.zeros((64,8),np.int64)
        scale=c.template_token_write_scale if template else c.role_scales[role]
        for path,m,_,_,layer,group in self.entries:
            x,z=self.captured[path]
            fired=mx.abs(z)>=c.amplitude_threshold
            self.frequency[path]=beta*self.frequency[path]+(1-beta)*fired
            gate=fired & (self.frequency[path]>=c.frequency_floor)
            pending[path]=(x,z,gate)
            # A stable disjoint assignment by sorted projection order and row.
            bucket=layer*2+int(group=="mlp")
            offsets=[mx.sum(mx.abs(z)[j::8]) for j in range(8)]
            sums[bucket]=sums[bucket]+mx.stack(offsets)
            counts[bucket]+=[len(range(j,z.size,8)) for j in range(8)]
        regions=mx.concatenate([sums[i]/mx.array(np.maximum(counts[i],1),mx.float32) for i in range(64)])
        mx.eval(regions,self.frequency)
        self.last_regions=np.array(regions)
        if c.enabled:
            proposed=dict(self.boundaries if self.pending_boundaries is None else self.pending_boundaries)
            for path,(x,y,context) in self.boundary_captured.items():
                record=boundaries.accumulate(proposed.get(path),x,y,context,scale,xp=mx)
                if record is not None:
                    mx.eval(*record.arrays.values())
                    proposed[path]=record
            if not all(bool(mx.all(mx.isfinite(a)).item()) for r in proposed.values() for a in r.arrays.values()):
                self.boundary_captured.clear();self.captured.clear()
                raise FloatingPointError("Nonfinite operation boundary; field retained")
            self.pending_boundaries=proposed
        self.boundary_captured.clear()
        self.captured.clear();self.token_count+=1
        if c.enabled:
            self.pending.append((pending,scale,self.token_count))
            if len(self.pending)>=c.commit_every_tokens:self.commit()

    def commit(self):
        if not self.pending:return
        started=time.monotonic()
        c=self.config;proposed={}; proposed_norms={}; changed=[]; candidates=[]
        proposed_directions=dict(self.directions)
        proposed_boundaries=self.boundaries if self.pending_boundaries is None else self.pending_boundaries
        layer_metrics=[mx.zeros((8,),mx.float32) for _ in range(32)]
        for path,m,*_ in self.entries:
            for activity,scale,_ in self.pending:
                x,z,gate=activity[path]
                coefficients=write_coefficients(z,gate,eta=c.eta,role_scale=scale,step_fraction_max=c.step_fraction_max,xp=mx)
                record=accumulate(proposed_directions.get(path),x,z,coefficients,scale,xp=mx)
                if record is not None:proposed_directions[path]=record
            if path in proposed_directions:mx.eval(*proposed_directions[path].arrays.values())
        if not all(bool(mx.all(mx.isfinite(a)).item()) for r in proposed_directions.values() for a in r.arrays.values()):
            self.pending.clear();self.pending_boundaries=None;raise FloatingPointError("Nonfinite direction state; old field retained")
        if c.baseline_pull==0 and all(c.eta*scale==0 for _,scale,_ in self.pending):
            self.directions=proposed_directions
            self.boundaries=proposed_boundaries
            self.receipts.append(dict(old_field_version=self.version,field_version=self.version,token_start=self.pending[0][2],token_end=self.pending[-1][2],changed_words=0,candidate_rows=0,baseline_pull=0,config_revision=self.config_revision,status='exact_noop'))
            self.receipts=self.receipts[-128:];self.pending.clear();self.pending_boundaries=None;return
        # Build all new arrays, force execution, then publish as one token boundary.
        # Current arrays and immutable anchors survive until the transaction commits.
        for path,m,_,_,layer,group in self.entries:
            current=m.weight
            norm=self.norm_cache.get(path)
            if norm is None:norm=row_norm(current,m.scales,m.biases)
            for activity,scale,token in self.pending:
                x,z,gate=activity[path]
                before=current
                seed=int.from_bytes(hashlib.sha256(f"{c.seed}:{token}:{path}".encode()).digest()[:4],"little")
                kwargs=dict(eta=c.eta,role_scale=scale,step_fraction_max=c.step_fraction_max,seed=seed,baseline_pull=c.baseline_pull,delta_n=1)
                if c.baseline_pull>0:kwargs['anchor']=self.anchor[path]
                if c.writer=="metal":
                    current=write(current,m.scales,m.biases,x,z,gate,norm,**kwargs)
                else:
                    if 'anchor' in kwargs:kwargs['anchor']=np.array(kwargs['anchor'])
                    out,_=reference_write(np.array(current),np.array(m.scales.astype(mx.float32)),np.array(m.biases.astype(mx.float32)),np.array(x),np.array(z),np.array(gate),**kwargs)
                    current=mx.array(out,mx.uint32)
                metrics=mx.sum(audit(before,current,m.scales,m.biases,x,z,gate,norm,**kwargs),axis=0)
                layer_metrics[layer]=layer_metrics[layer]+metrics
                norm=row_norm(current,m.scales,m.biases)
                candidates.append(mx.sum(gate))
            changed.append(mx.sum(current!=m.weight))
            proposed[path]=current
            # Materialize per projection to bound lazy graph/transient lifetime.
            mx.eval(current,norm,changed[-1],layer_metrics[layer])
            proposed_norms[path]=norm
        mx.eval(proposed)
        if not bool(mx.all(mx.stack([mx.all(mx.isfinite(n)) for n in proposed_norms.values()])).item()):
            self.pending.clear();self.pending_boundaries=None;raise FloatingPointError("Nonfinite proposed field; old weights retained")
        count=int(mx.sum(mx.stack(changed)).item())
        measured=np.array(mx.stack(layer_metrics))
        if not np.isfinite(measured).all():
            self.norm_cache.clear();self.pending.clear();self.pending_boundaries=None;raise FloatingPointError('Nonfinite write metrics; old weights retained')
        for path,m,*_ in self.entries:m.weight=proposed[path]
        self.directions=proposed_directions
        self.boundaries=proposed_boundaries
        self.norm_cache=proposed_norms
        previous=self.version;self.version+=1;self.changed_words+=count
        names=('candidate_row_events','changed_row_events','changed_codes','candidate_codes','saturated_codes','proposal_squared_norm','realized_squared_norm','quantization_squared_error')
        per_layer=[dict(layer=i,**{k:(int(v) if j<5 else float(v)) for j,(k,v) in enumerate(zip(names,row))}) for i,row in enumerate(measured)]
        self.receipts.append(dict(old_field_version=previous,field_version=self.version,token_start=self.pending[0][2],token_end=self.pending[-1][2],changed_words=count,candidate_rows=int(mx.sum(mx.stack(candidates)).item()),baseline_pull=c.baseline_pull,config_revision=self.config_revision,per_layer=per_layer,seconds=time.monotonic()-started,seed=c.seed,rng_counter_range=[self.pending[0][2],self.pending[-1][2]],status='committed'))
        self.receipts=self.receipts[-128:]
        self.pending.clear();self.pending_boundaries=None

    def reset(self,kind,retain_traces=False):
        self.pending.clear();self.pending_boundaries=None;self.captured.clear();self.boundary_captured.clear()
        if kind in ("weights","entire_session"):
            for path,m,*_ in self.entries:m.weight=self.anchor[path]
            self.norm_cache.clear();self.version+=1
        if kind in ("activity_traces","entire_session") or (kind=="weights" and not retain_traces):
            self.frequency={p:mx.zeros_like(f) for p,f in self.frequency.items()}
            self.receipts=[]
            self.directions={}
            self.boundaries={}
        if kind=="entire_session":self.token_count=0

    def diagnostics(self):
        return dict(direction_projections=len(self.directions),direction_coverage="quantized_projections",reverse_boundary_operations=len(self.boundaries),field_version=self.version,consumed_tokens=self.token_count,eligible_layers=32,eligible_projections=len(self.entries),changed_words_total=self.changed_words,last_receipts=self.receipts[-8:],baseline_pull=self.config.baseline_pull,read_only=self.config.read_only,field_enabled=self.config.enabled,commit_every_tokens=self.config.commit_every_tokens,precision="fp32 proposal; uint32 packed affine4; fixed group64 scales/offsets",realized_step_bound=False)


