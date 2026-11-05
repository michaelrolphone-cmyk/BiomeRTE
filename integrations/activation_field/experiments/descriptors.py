"""Template-held-out trajectory retrieval and calibrated validity evaluation.

Labels come from exact numeric validators, never from model fluency. These
small synthetic reasoning tasks qualify the measurement pipeline; they do not
establish broad hallucination detection or semantic equivalence.
"""
import json,random,time
from pathlib import Path
import numpy as np
from .storage import atomic_json,prepare,run_lock,digest
from activation_field.spectra import dtw


def corpus(seed=42,count=5):
    rows=[]
    templates={
      'train':('Work through the following example.','The intermediate calculation is','The final result is'),
      'validation':('Check this proposed solution.','Computing the intermediate value gives','This yields'),
      'test':('Consider this reasoning record.','After evaluating the stated operation we obtain','Therefore the reported answer is')}
    for split,phrases in templates.items():
        for domain in ('arithmetic','code','constraint'):
            for pattern in ('accumulate','branch','correction'):
                for i in range(count):
                    rng=random.Random(f'{seed}:{split}:{domain}:{pattern}:{i}')
                    a,b,c=[rng.randrange(10,40) for _ in range(3)]
                    intermediate=a+b
                    if pattern=='accumulate':answer=intermediate+c;operation=f'x = {a} + {b}; result = x + {c}'
                    elif pattern=='branch':answer=intermediate-c if intermediate>50 else intermediate+c;operation=f'x = {a} + {b}; result = x - {c} if x > 50 else x + {c}'
                    else:answer=a+c;operation=f'x = {a} + {b}; x = x - {b}; result = x + {c}'
                    scope={};exec(compile(operation,'synthetic-oracle','exec'),{'__builtins__':{}},scope)
                    assert scope['result']==answer
                    if domain=='code':problem='Evaluate this Python program: '+operation+'. State result.'
                    elif domain=='constraint':problem=f'A counter starts at {a}. Add {b}. '+({'accumulate':f'Add {c}.','branch':f'If the counter exceeds 50, subtract {c}; otherwise add {c}.','correction':f'The addition of {b} was cancelled. Add {c} instead.'}[pattern])+' Find the final counter.'
                    else:problem=({'accumulate':f'Calculate ({a}+{b})+{c}.','branch':f'Compute {a}+{b}, subtracting {c} when the sum exceeds 50 and adding {c} otherwise.','correction':f'An initial sum {a}+{b} used the wrong second number. Replace {b} with {c} and recalculate.'}[pattern])
                    for valid in (True,False):
                        claimed=answer if valid else answer+1
                        trace=f'{phrases[0]} {problem}\n{phrases[1]} {intermediate}. We retain the operation order, check the stated condition, and apply the final operation. {phrases[2]} {claimed}.\n'
                        rows.append(dict(id=f'{split}-{domain}-{pattern}-{i}-{int(valid)}',conversation=f'{split}-{domain}-{pattern}-{i}',template_id=split+'-'+domain+'-'+pattern,split=split,domain=domain,pattern=pattern,difficulty=1,field_age=0,valid=claimed==answer,expected=answer,claimed=claimed,trace=trace))
    data=dict(schema_version=1,seed=seed,rows=rows,padding='This is a synthetic reasoning record. The operations and condition should be checked in their stated order. ')
    data['sha256']=digest(data);return data


def channels(descriptor):
    return dict(structural=np.array(descriptor['eigenvalues']+list(descriptor['heat_trace'].values())+[descriptor['components'],descriptor['isolated_nodes']],float),
                directed=np.array(descriptor['singular_values']+[descriptor['antisymmetric_norm']]+np.asarray(descriptor['layer_flow']).reshape(-1).tolist(),float),
                temporal=np.array(descriptor['layer_mean']+descriptor['layer_variance']+descriptor['self_persistence']+sum([descriptor['autocorrelation'][str(k)] for k in (1,2,4,8)],[]),float))


def sigmoid(x):return 1/(1+np.exp(-np.clip(x,-40,40)))


def fit(x,y,penalty=1.):
    # Frozen ridge logistic model; no test-based hyperparameter selection.
    from scipy.optimize import minimize
    x=np.column_stack([np.ones(len(x)),x]);w=np.zeros(x.shape[1]);mask=np.ones_like(w);mask[0]=0
    def objective(w):
        z=x@w;p=sigmoid(z)
        return float(np.mean(np.logaddexp(0,z)-y*z)+penalty*np.sum((w*mask)**2)/2),x.T@(p-y)/len(y)+penalty*w*mask
    result=minimize(objective,w,jac=True,method='L-BFGS-B')
    if not result.success:raise ValueError('Classifier did not converge: '+result.message)
    return result.x


def metrics(y,p):
    positive=p[y==1];negative=p[y==0]
    if not len(positive) or not len(negative):raise ValueError('Both validation classes required')
    auc=float(np.mean((positive[:,None]>negative[None,:])+.5*(positive[:,None]==negative[None,:])))
    pred=p>=.5;tp=int(np.sum(pred&(y==1)));fp=int(np.sum(pred&(y==0)));fn=int(np.sum(~pred&(y==1)))
    bins=[];ece=0.
    for lo,hi in zip(np.linspace(0,1,11)[:-1],np.linspace(0,1,11)[1:]):
        selected=(p>=lo)&((p<=hi) if hi==1 else (p<hi))
        if selected.any():
            confidence=float(p[selected].mean());frequency=float(y[selected].mean())
            ece+=float(selected.mean())*abs(confidence-frequency);bins.append(dict(lower=float(lo),upper=float(hi),count=int(selected.sum()),confidence=confidence,observed=frequency))
    return dict(auroc=auc,precision=tp/max(tp+fp,1),recall=tp/max(tp+fn,1),brier=float(np.mean((p-y)**2)),ece=ece,calibration_bins=bins,count=len(y))


def evaluate(rows):
    if any(not r.get('descriptors') or any('error' in d for d in r['descriptors']) for r in rows):raise ValueError('Incomplete/error descriptor coverage')
    template_sets=[{r['template_id'] for r in rows if r['split']==s} for s in ('train','validation','test')]
    if any(template_sets[i]&template_sets[j] for i in range(3) for j in range(i)):raise ValueError('Template leakage')
    conversations=[{r['conversation'] for r in rows if r['split']==s} for s in ('train','validation','test')]
    if any(conversations[i]&conversations[j] for i in range(3) for j in range(i)):raise ValueError('Conversation leakage')
    bank={k:[np.stack([channels(d)[k] for d in r['descriptors']]) for r in rows] for k in ('structural','directed','temporal')}
    train=np.array([i for i,r in enumerate(rows) if r['split']=='train']);val=np.array([i for i,r in enumerate(rows) if r['split']=='validation']);test=np.array([i for i,r in enumerate(rows) if r['split']=='test'])
    if min(len(train),len(val),len(test))==0:raise ValueError('All three splits required')
    y=np.array([r['valid'] for r in rows],float)
    standardized={};normalization={}
    for k,sequences in bank.items():
        training=np.concatenate([sequences[i] for i in train]);mean=training.mean(0);scale=np.maximum(training.std(0),1e-6)
        standardized[k]=[(s-mean)/scale for s in sequences];normalization[k]=dict(mean=mean.tolist(),scale=scale.tolist(),fit_split='train')
    features={k:np.stack([s.mean(0) for s in v]) for k,v in standardized.items()}
    features['graph']=np.concatenate([features[k] for k in ('structural','directed','temporal')],axis=1)
    features['length']=np.array([[r['token_count']] for r in rows],float)
    features['activation_mean']=np.stack([r['activation_mean'] for r in rows])
    shuffled=np.arange(len(rows));rng=np.random.default_rng(42)
    for idx in (train,val,test):shuffled[idx]=rng.permutation(idx)
    features['shuffled']=features['graph'][shuffled]
    if all('persistence' in d for r in rows for d in r['descriptors']):
        ph=np.array([[np.mean([d['persistence'][h][stat] for d in r['descriptors']]) for h in ('H0','H1') for stat in ('finite_count','essential_count','total_persistence','max_persistence')] for r in rows])
        features['graph_plus_homology']=np.concatenate([features['graph'],ph],axis=1)
    reports={}
    for name,x in features.items():
        mean=x[train].mean(0);scale=np.maximum(x[train].std(0),1e-6);x=(x-mean)/scale
        w=fit(x[train],y[train]);raw=np.column_stack([np.ones(len(x)),x])@w
        calibration=fit(raw[val,None],y[val],penalty=.01)
        probabilities=sigmoid(calibration[0]+calibration[1]*raw)
        retrieval=[]
        for i in test:
            candidates=[j for j in train if rows[j]['domain']!=rows[i]['domain'] and rows[j]['difficulty']==rows[i]['difficulty'] and rows[j]['field_age']==rows[i]['field_age']]
            if not candidates:continue
            distance=[float(np.linalg.norm(x[i]-x[j])) for j in candidates];j=candidates[int(np.argmin(distance))]
            retrieval.append(dict(query=rows[i]['id'],match=rows[j]['id'],same_event_pattern=rows[i]['pattern']==rows[j]['pattern'],token_difference=rows[i]['token_count']-rows[j]['token_count']))
        reports[name]=dict(coherence=metrics(y[test],probabilities[test]),cross_domain_retrieval=dict(accuracy=float(np.mean([r['same_event_pattern'] for r in retrieval])),pairs=retrieval),classifier_weights=w.tolist(),normalization=dict(mean=mean.tolist(),scale=scale.tolist()),calibration=calibration.tolist())
    # Trajectory distance channels stay separate; no implication of injectivity.
    temporal_retrieval=[]
    for i in test:
        candidates=[j for j in train if rows[j]['domain']!=rows[i]['domain']]
        distances={k:[dtw(standardized[k][i],standardized[k][j]) for j in candidates] for k in standardized}
        temporal_retrieval.append(dict(query=rows[i]['id'],channels={k:dict(match=rows[candidates[int(np.argmin(v))]]['id'],distance=min(v)) for k,v in distances.items()}))
    return dict(schema_version=1,label='trajectory_novelty_unvalidated',generalization_scope='fixed synthetic templates; validator-defined answer correctness',methods=reports,trajectory_retrieval=temporal_retrieval,channel_normalization=normalization,counts={s:sum(r['split']==s for r in rows) for s in ('train','validation','test')},field_age=0,heldout_auroc_target_met=reports['graph']['coherence']['auroc']>=.75)


def run(config,resume=False):
    from activation_field.runtime import Engine
    from activation_field.config import FieldConfig
    from activation_field.manifest import read_manifest
    from activation_field.admission import model_slot
    root=Path(config['output']);data=corpus(config.get('corpus_seed',42),config.get('examples_per_template',5));manifest=read_manifest(config['manifest'])
    with run_lock(root):
        identity=prepare(root,config,data,manifest['baseline_hash'],resume)
        path=root/'traces.json';rows=json.loads(path.read_text()) if path.exists() else [];done={r['id'] for r in rows}
        def progress(state,**extra):atomic_json(root/'progress.json',dict(status=state,kind='descriptors',completed_traces=len(rows),expected_traces=len(data['rows']),identity=identity,updated_at=time.time(),**extra))
        try:
            for item in data['rows']:
                if item['id'] in done:continue
                progress('waiting_for_model',trace=item['id'])
                with model_slot():
                    engine=Engine(manifest,FieldConfig(enabled=False,persistent_homology=config.get('persistent_homology',False)))
                    try:
                        ids=engine.tokenizer.encode(item['trace'],add_special_tokens=False)
                        padding=engine.tokenizer.encode(data['padding'],add_special_tokens=False)
                        # Equal fixed observation window includes the whole trace.
                        # Any longer trace uses its exact length rounded to a stride.
                        target=max(128,128+32*int(np.ceil(max(0,len(ids)-128)/32)))
                        ids+= [padding[i%len(padding)] for i in range(target-len(ids))]
                        region_sum=np.zeros(512)
                        for token in ids:
                            engine.consume(token);region_sum+=engine.field.last_regions
                        state=engine.trajectory.state()
                        if any('error' in d for d in state['descriptors']):raise ValueError('Descriptor computation failed')
                        rows.append(dict(**{k:v for k,v in item.items() if k!='trace'},token_count=len(ids),activation_mean=(region_sum/len(ids)).tolist(),descriptors=state['descriptors']))
                        atomic_json(path,rows);done.add(item['id']);progress('running',trace=item['id'])
                    finally:engine.close()
            result=evaluate(rows);atomic_json(root/'descriptor-report.json',result);progress('complete');return str(root)
        except Exception as exc:
            progress('interrupted',error=type(exc).__name__+': '+str(exc));raise
