"""Stratified streaming observations; adaptive signed ellipse residual fitting.

The temporary observations are encrypted resumable job state, never AFWT members.
Parameters are scored only after float32 encode/decode. Final diagnostics use all
coordinates, including an untouched deterministic diagnostic partition.
"""
import dataclasses
import math
import time
from pathlib import Path
import numpy as np
from ..checkpoint import Vault
from .common import TransformError, digest, check_cancel
from .coordinates import split_ids, tile_coordinates
from .regions import Region, evaluate_delta
from .format import encode, decode, save_transform, inspect_transform

DEFAULTS = dict(schema_version=1, function_family='elliptic-value-v1', target_bytes=1000000,
                size_goal_mode='advisory', coordinate_rule='tensor-cell-center-v1', fit_signed_delta=True,
                value_modes=['disabled', 'affine', 'windowed_affine'], falloff_exponents=[1, 2, 4],
                unchanged_loss_weight=1., split_fractions=[.8, .1, .1], split_seed=20261104,
                size_penalty='adaptive_frontier', parameter_encoding='float32-v1', restore_rounding='nearest_ties_even',
                restore_native_state='empty', evaluation_field_writes=False, evaluation_decay=False,
                operator_max_runtime_seconds=None, operator_max_regions=None,
                convergence_relative_improvement=1e-4, geometry_relative_tolerance=.05,
                sampling_memory_fraction=.001, tile_bytes=None)


def configuration(value=None):
    value = value or {}
    if set(value)-set(DEFAULTS): raise ValueError('Unknown transform configuration: '+str(sorted(set(value)-set(DEFAULTS))))
    c = dict(DEFAULTS, **value)
    for key in ('schema_version','function_family','size_goal_mode','coordinate_rule','fit_signed_delta','size_penalty',
                'parameter_encoding','restore_rounding','restore_native_state','evaluation_field_writes','evaluation_decay'):
        if c[key] != DEFAULTS[key]: raise ValueError('Unsupported '+key)
    if not c['value_modes'] or set(c['value_modes'])-{'disabled','affine','windowed_affine'}: raise ValueError('Invalid value modes')
    if not c['falloff_exponents'] or set(c['falloff_exponents'])-{1,2,4}: raise ValueError('Invalid falloff exponents')
    f = c['split_fractions']
    if len(f)!=3 or any(not np.isfinite(v) or v<=0 for v in f) or not math.isclose(sum(f),1.,abs_tol=1e-12): raise ValueError('Invalid split fractions')
    for k in ('target_bytes','split_seed'):
        if type(c[k]) is not int or c[k]<0 or (k=='target_bytes' and c[k]==0): raise ValueError('Invalid '+k)
    for k in ('unchanged_loss_weight','convergence_relative_improvement','geometry_relative_tolerance','sampling_memory_fraction'):
        if not np.isfinite(c[k]) or c[k]<0: raise ValueError('Invalid '+k)
    if not 0<c['geometry_relative_tolerance']<1 or not 0<c['sampling_memory_fraction']<1: raise ValueError('Invalid numerical convergence/workspace policy')
    for k in ('operator_max_regions','tile_bytes'):
        if c[k] is not None and (type(c[k]) is not int or c[k]<1): raise ValueError('Invalid '+k)
    if c['operator_max_runtime_seconds'] is not None and (not np.isfinite(c['operator_max_runtime_seconds']) or c['operator_max_runtime_seconds']<=0): raise ValueError('Invalid runtime budget')
    return c


def observations(source, d, c):
    # Bernoulli inclusion is independent of held-out split; inclusion weights are
    # explicit. Workspace, not a region/runtime cap, determines sample density.
    stats=source.stats[d['id']]
    workspace=max(4096, int(source.tile_bytes*c['sampling_memory_fraction']/.01))
    desired=max(1, workspace//(8*12))
    p={label:min(1., desired/max(1,stats[label])) for label in ('changed','unchanged')}
    collected=[]
    rng=np.random.default_rng(c['split_seed']+d['id'])
    for t in source.tiles(d):
        changed=t['target_codes']!=t['base_codes']
        probabilities=np.where(changed,p['changed'],p['unchanged'])
        mask=rng.random(changed.shape)<probabilities
        splits=split_ids(d['path'],d['logical_shape'],t['start'],len(t['base']),c['split_seed'],c['split_fractions'])
        mask &= splits != 2  # Final diagnostic coordinates are never observed here.
        rr,cc=np.nonzero(mask)
        if len(rr):
            collected.append(np.column_stack([rr+t['start'],cc,t['base'][mask],(t['target']-t['base'])[mask],changed[mask],splits[mask],probabilities[mask]]))
    a=np.concatenate(collected) if collected else np.empty((0,7))
    return a,dict(method='Bernoulli strata sampling',inclusion_probabilities=p,workspace_bytes=workspace,
                  sample_count=len(a),estimand='equal tensor, separate changed/unchanged mean squared normalized error',
                  split_hash='sha256-name-seed/splitmix64-logical-index-v1')


def sample_loss(regions, d, a, split, c):
    mask=a[:,5]==split
    if not mask.any(): return None
    b=a[mask];pred=evaluate_delta(regions,d,b[:,0],b[:,1],b[:,2]);err=((pred-b[:,3])/d['s_m'])**2
    total=0.
    for changed,weight in [(True,1.),(False,c['unchanged_loss_weight'])]:
        chosen=b[:,4]==changed
        if chosen.any():
            inclusion=1/b[chosen,6]
            total+=weight*float(np.sum(err[chosen]*inclusion)/np.sum(inclusion))
    return total


def overlaps(a, b):
    if a.tensor_id != b.tensor_id:return False
    x,y=a.support_bounds(),b.support_bounds()
    return x[0]<=y[1] and y[0]<=x[1] and x[2]<=y[3] and y[2]<=x[3]


def refit_neighborhood(current, d, a, c, workspace_bytes=None):
    """Solve local overlaps jointly; retain bounded-memory coordinate fallback."""
    if not current:return current
    affected=[r.id for r in current if overlaps(r,current[-1])]
    score=sample_loss(current,d,a,0,c)
    selected=[r for r in current if r.id in affected]
    others=[r for r in current if r.id not in affected]
    problem=training_problem(others,d,a,c)
    if problem is None:return current
    x,y,u,residual,weights=problem
    modes=[(r,bool(r.b or r.window)) for r in selected]
    columns=sum(1+int(affine) for r,affine in modes)
    workspace=workspace_bytes if workspace_bytes is not None else max(a.nbytes*8,columns*columns*32)
    if columns*columns*32<=workspace:
        def design(start,end):
            pieces=[]
            for r,affine in modes:
                phi,psi=r.basis(x[start:end],y[start:end],u[start:end]);pieces.append(phi)
                if affine:pieces.append(psi)
            return np.column_stack(pieces)*np.sqrt(weights[start:end])[:,None]
        if len(x)*columns*32<=workspace:
            matrix=design(0,len(x))
            coef=np.linalg.lstsq(matrix,residual*np.sqrt(weights),rcond=None)[0]
        else:
            # Streaming sufficient statistics: no parameter-by-model matrix.
            gram=np.zeros((columns,columns));rhs=np.zeros(columns)
            rows=max(1,(workspace-columns*columns*32)//max(1,columns*32))
            for start in range(0,len(x),rows):
                end=min(len(x),start+rows);matrix=design(start,end)
                gram+=matrix.T@matrix;rhs+=matrix.T@(residual[start:end]*np.sqrt(weights[start:end]))
            scales=np.sqrt(np.maximum(np.diag(gram),np.finfo(float).tiny))
            normalized=gram/scales[:,None]/scales[None,:]
            coef=np.linalg.lstsq(normalized,rhs/scales,rcond=np.finfo(float).eps*max(len(x),columns))[0]/scales
        index=0;revised=[]
        for r,affine in modes:
            revised.append(dataclasses.replace(r,a=float(coef[index]),b=float(coef[index+1]) if affine else 0.))
            index+=1+int(affine)
        candidate=sorted(others+revised,key=lambda r:r.id)
        return candidate if sample_loss(candidate,d,a,0,c)<=score else current
    while True:
        previous=score
        for rid in affected:
            region=next(r for r in current if r.id==rid)
            others=[r for r in current if r.id!=rid]
            revised=fit_coefficients(region,others,d,a,c,region.b!=0 or region.window!=0)
            if revised is None:continue
            candidate=sorted(others+[revised],key=lambda r:r.id)
            loss=sample_loss(candidate,d,a,0,c)
            if loss<=score:current,score=candidate,loss
        if previous-score<=c['convergence_relative_improvement']*max(previous,np.finfo(float).tiny):break
    return current


def training_problem(current,d,a,c):
    b=a[a[:,5]==0]
    if not len(b):return None
    x0=(b[:,0]+.5)/d['logical_shape'][0];x1=(b[:,1]+.5)/d['logical_shape'][1];u=b[:,2]/d['s_m']
    residual=(b[:,3]-evaluate_delta(current,d,b[:,0],b[:,1],b[:,2]))/d['s_m']
    weights=np.zeros(len(b))
    for changed,w in [(True,1.),(False,c['unchanged_loss_weight'])]:
        mask=b[:,4]==changed
        if mask.any():weights[mask]=w/b[mask,6]/np.sum(1/b[mask,6])
    return x0,x1,u,residual,weights


def training_score(region,problem):
    x0,x1,u,residual,weights=problem
    phi,psi=region.basis(x0,x1,u)
    return float(np.sum(weights*(phi*region.a+psi*region.b-residual)**2))


def fit_coefficients(region, current, d, a, c, affine, problem=None):
    problem=training_problem(current,d,a,c) if problem is None else problem
    if problem is None:return None
    x0,x1,u,residual,weights=problem
    phi,_=region.basis(x0,x1,u)
    weighted_phi=weights*phi;effective=weighted_phi*phi;total=float(effective.sum())
    if not total:return None
    intercept=float(np.sum(weighted_phi*residual)/total);slope=0.;center=0.
    if affine:
        z=u-region.mu;center=float(np.sum(effective*z)/total);centered=z-center
        variance=float(np.sum(effective*centered**2))
        # Centered sufficient statistics avoid an N-by-2 SVD and its condition
        # squaring. Rank tolerance is floating precision, not a fit-quality gate.
        tolerance=np.finfo(float).eps*total*max(1.,float(np.max(np.abs(z)))**2)
        if variance>tolerance:slope=float(np.sum(weighted_phi*centered*residual)/variance)
    return dataclasses.replace(region,a=intercept-slope*center,b=slope)


def propose(current, d, a, c, region_id, bounds):
    b=a[a[:,5]==0]
    if not len(b):return []
    problem=training_problem(current,d,a,c)
    x=np.column_stack([(b[:,0]+.5)/d['logical_shape'][0],(b[:,1]+.5)/d['logical_shape'][1]])
    residual=(b[:,3]-evaluate_delta(current,d,b[:,0],b[:,1],b[:,2]))/d['s_m']
    inside=(x[:,0]>=bounds[0])&(x[:,0]<=bounds[1])&(x[:,1]>=bounds[2])&(x[:,1]<=bounds[3])
    candidates=[]
    for sign in (-1,1):
        mask=inside&(residual*sign>0)
        if not mask.any():continue
        weight=np.abs(residual[mask])/b[mask,6];points=x[mask]
        center=np.average(points,axis=0,weights=weight)
        cov=((points-center)*weight[:,None]).T@(points-center)/weight.sum()
        vals,vec=np.linalg.eigh(cov);order=np.argsort(vals)[::-1];vals=vals[order];vec=vec[:,order]
        radii=np.maximum(np.sqrt(np.maximum(vals,0))*3,1/np.array(d['logical_shape']))
        angle=math.atan2(vec[1,0],vec[0,0]);values=b[mask,2]/d['s_m'];mu=float(np.average(values,weights=weight))
        hv=max(float(np.sqrt(np.average((values-mu)**2,weights=weight))*3),1e-6)
        for mode in c['value_modes']:
            for exponent in c['falloff_exponents']:
                best=None;best_loss=float('inf')
                for factor in (.5,1.,2.):
                    region=Region(region_id,d['id'],*center,*np.log(radii*factor),angle,0.,0.,mu,hv if mode=='windowed_affine' else 1.,exponent,int(mode=='windowed_affine'))
                    region=fit_coefficients(region,current,d,a,c,mode!='disabled',problem)
                    if region is None:continue
                    score=training_score(region,problem)
                    if score<best_loss:best,best_loss=region,score
                if best is not None:
                    best=refine_geometry(best,current,d,a,c,mode!='disabled',problem)
                    candidates.append((best,mode))
    return candidates


def refine_geometry(region,current,d,a,c,affine,problem=None):
    """Training-only coordinate pattern search; convergence is explicit."""
    problem=training_problem(current,d,a,c) if problem is None else problem
    score=training_score(region,problem);step=.25
    while step>=c['geometry_relative_tolerance']:
        improved=False
        scales=dict(c0=math.exp(region.log_h0),c1=math.exp(region.log_h1),log_h0=1.,log_h1=1.,angle=math.pi)
        if affine:scales['mu']=max(abs(region.mu),1.)
        if region.window:scales['hv']=region.hv
        for key,scale in scales.items():
            for sign in (-1,1):
                value=getattr(region,key)+sign*step*scale
                if key=='hv' and value<=0:continue
                trial=dataclasses.replace(region,**{key:value})
                trial=fit_coefficients(trial,current,d,a,c,affine,problem)
                if trial is None:continue
                loss=training_score(trial,problem)
                if score-loss>c['convergence_relative_improvement']*max(score,np.finfo(float).tiny):
                    region,score,improved=trial,loss,True
        if not improved:step/=2
    return region


def fit(source, output, config=None, resume=False, progress=None, cancel=None):
    c=configuration(config);output=Path(output);output.mkdir(parents=True,exist_ok=True,mode=0o700)
    vault=Vault(output/'private');started=time.monotonic()
    source.characterize(progress)
    identity=digest(dict(source=source.identity,config=c,descriptors=source.descriptors))
    statefile=vault.root/'fit.enc'
    if statefile.exists():
        if not resume:raise FileExistsError('Use explicit resume for this fit')
        state=vault.load_json('fit.enc')
        if state['identity']!=identity:raise TransformError('source_version_changed','Fit source/configuration changed')
    else:
        state=dict(identity=identity,regions=[],tensor_index=0,candidates=[],sampling={},elapsed_seconds=0.,frontier=[],penalty_trials=[])
    regions=[Region(**r) for r in state['regions']];manifest=source.manifest(c)
    # The complete mapping is already in every artifact. Do not copy it into
    # every journal entry and repeatedly encrypt megabytes of duplicate data.
    for candidate in state['candidates']:
        for key in ('baseline_ref','coverage','provenance'):candidate.pop(key,None)
    revision=getattr(source.engine,'manifest',{}).get('source_revision','fixture')
    history=state.setdefault('implementation_history',[])
    if not history or history[-1]!=revision:history.append(revision)
    def checkpoint():
        state['regions']=[r.to_dict() for r in regions];vault.save_json('fit.enc',state)
    def retain(reason, validation):
        candidate_id='candidate-'+digest([manifest,[r.to_dict() for r in regions]])[:20]
        receipt=save_transform(manifest,regions,output/(candidate_id+'.afwt.enc'))
        compact={k:v for k,v in receipt.items() if k not in ('baseline_ref','coverage','provenance')}
        record=dict(candidate_id=candidate_id,validation_loss=validation,selection_reason=reason,**compact)
        if not any(r['candidate_id']==candidate_id for r in state['candidates']):state['candidates'].append(record)
        valid=[r for r in state['candidates'] if r['validation_loss'] is not None]
        state['frontier']=[r['candidate_id'] for r in valid if not any(q['bytes']<=r['bytes'] and q['validation_loss']<=r['validation_loss'] and (q['bytes']<r['bytes'] or q['validation_loss']<r['validation_loss']) for q in valid)]
        if validation is not None:
            # Explicit geometric size penalties; preserve the zero-penalty result.
            initial=validation*c['target_bytes']/max(1,receipt['bytes'])
            for penalty in (0.,initial/4,initial,initial*4):
                selected=min(valid,key=lambda q:q['validation_loss']+penalty*q['bytes']/c['target_bytes'])
                state['penalty_trials'].append(dict(penalty=penalty,selected=selected['candidate_id'],reason='geometric frontier sweep; no new nondominated candidate => convergence'))
        checkpoint()
    # Observations for each tensor are captured independently in an encrypted
    # temporary checkpoint. Fitting never serializes them into portable output.
    state.setdefault('tensor_losses',{})
    checkpoint()
    for d in source.descriptors:
        if not source.stats[d['id']]['changed'] or str(d['id']) in state['tensor_losses']:continue
        check_cancel(cancel)
        name='observations-'+str(d['id'])
        if (vault.root/name).exists():
            metadata=vault.manifest(name)['metadata']
            if metadata.get('identity')!=identity:raise TransformError('source_version_changed','Observation source/configuration changed')
            a=dict(vault.arrays(name))['observations'];sampling=metadata['sampling']
        else:
            a,sampling=observations(source,d,c)
            vault.save(name,dict(baseline_hash=source.baseline_ref['baseline_hash'],sampling=sampling,identity=identity),[('observations',a)])
        state['sampling'][d['path']]=sampling
        state['tensor_losses'][str(d['id'])]=sample_loss(regions,d,a,1,c)
        checkpoint()
    def aggregate_loss():
        losses=[v for v in state['tensor_losses'].values() if v is not None]
        return sum(losses)/len(losses) if losses else 0.
    if not state['candidates']:retain('identity initialization',aggregate_loss())
    try:
        for index in range(state['tensor_index'],len(source.descriptors)):
            check_cancel(cancel);d=source.descriptors[index]
            if source.stats[d['id']]['changed']==0:
                state['tensor_index']=index+1;checkpoint();continue
            a=dict(vault.arrays('observations-'+str(d['id'])))['observations']
            if not np.any(a[:,5]==1):
                state.setdefault('unfitted',[]).append(dict(path=d['path'],reason='no validation observations'))
                state['tensor_index']=index+1;checkpoint();continue
            pending=state.get('pending_bounds',[[0.,1.,0.,1.]])
            local_loss=sample_loss(regions,d,a,1,c)
            # A complete tensor is an optimization unit. Its other-tensor score
            # is recorded separately; candidate frontier is later fully rescored.
            while pending:
                check_cancel(cancel)
                if c['operator_max_runtime_seconds'] is not None and time.monotonic()-started+state['elapsed_seconds']>=c['operator_max_runtime_seconds']:
                    raise TransformError('cancelled','operator runtime budget')
                if c['operator_max_regions'] is not None and len(regions)>=c['operator_max_regions']:
                    raise TransformError('cancelled','operator region budget')
                bounds=pending.pop(0);best=None;best_loss=local_loss
                for proposal,mode in propose(regions,d,a,c,max([r.id for r in regions],default=-1)+1,bounds):
                    jointly_fitted=refit_neighborhood(regions+[proposal],d,a,c,source.tile_bytes)
                    decoded=decode(encode(manifest,jointly_fitted))[1]
                    score=sample_loss(decoded,d,a,1,c)
                    if score<best_loss:
                        best,best_loss=(decoded,mode),score
                improvement=(local_loss-best_loss)/max(local_loss,np.finfo(float).tiny)
                if best is not None and improvement>c['convergence_relative_improvement']:
                    regions,mode=best;local_loss=best_loss
                    # Prune redundant overlapping primitives only when decoded
                    # validation improves, retaining prior candidates separately.
                    for region in list(regions):
                        if region.tensor_id!=d['id']:continue
                        reduced=[r for r in regions if r.id!=region.id]
                        reduced_loss=sample_loss(reduced,d,a,1,c)
                        if reduced_loss<=local_loss:regions,local_loss=reduced,reduced_loss
                    # Refine residual structure adaptively, with global coordinates
                    # and no clipping of proposed ellipses at partition boundaries.
                    widths=[(bounds[1]-bounds[0])*d['logical_shape'][0],(bounds[3]-bounds[2])*d['logical_shape'][1]]
                    axis=int(np.argmax(widths));left=axis*2;mid=(bounds[left]+bounds[left+1])/2
                    if widths[axis]>1:
                        b1,b2=bounds.copy(),bounds.copy();b1[left+1]=mid;b2[left]=mid;pending.extend([b1,b2])
                    state['pending_bounds']=pending;state['tensor_index']=index
                    state['tensor_losses'][str(d['id'])]=local_loss
                    retain('decoded validation improvement; '+mode,aggregate_loss())
                state['pending_bounds']=pending;checkpoint()
                if progress:progress(dict(stage='fit',tensor=d['path'],regions=len(regions),validation_loss=local_loss,remaining_proposals=len(pending)))
            state['tensor_index']=index+1;state.pop('pending_bounds',None);checkpoint()
        state['stop_reason']='exhausted improving residual proposals';state['status']='succeeded'
    except (InterruptedError,TransformError) as exc:
        if isinstance(exc,TransformError) and exc.code!='cancelled':raise
        state['status']='cancelled';state['stop_reason']=str(exc)
        raise
    finally:
        state['elapsed_seconds']+=time.monotonic()-started;checkpoint()
    # All retained functions remain usable, including above-goal alternatives.
    # A full scan supplies honest numerical metrics for the final candidate.
    from .evaluate import numerical_scan
    final=state['candidates'][-1]
    transform=decode(encode(manifest,regions))
    report=numerical_scan(source,transform,c,progress)
    report.update(candidate_id=final['candidate_id'],candidate_hash=final['sha256'],fitting_seconds=state['elapsed_seconds'],
                  stop_reason=state['stop_reason'],source=source.identity,sampling=state['sampling'],
                  candidate_selection='operator; no size-only default',behavioral_evaluation='unexecuted')
    report['source_signed_statistics']=source.stats
    report['implementation_history']=state['implementation_history']
    vault.save_json('report.enc',report)
    state['final_report']=report;checkpoint()
    return dict(status=state['status'],stop_reason=state['stop_reason'],candidates=state['candidates'],report=report,
                coverage=manifest['coverage'],baseline_ref=source.baseline_ref)
