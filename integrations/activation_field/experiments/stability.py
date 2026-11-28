"""Durable full-model stability study. Model admission yields every checkpoint."""
import json,time,uuid
from pathlib import Path
import numpy as np
from .storage import atomic_json,prepare,run_lock,digest


def suite():
    items=[]
    for a,b in ((7,4),(13,8),(23,11),(19,6)):
        items.append(dict(kind='arithmetic',prompt=f'Return only the integer: ({a} + {b}) * 3 - {b}.',answer=str((a+b)*3-b)))
        # Known fixed program, executed locally to derive its oracle. Model text
        # is scored as data; generated arbitrary code is never executed.
        code=f'values = [{a}, {b}, 3]\nresult = sum(x * x for x in values)\nprint(result)'
        scope={};exec(compile(code.replace('print(result)',''),'fixed-suite','exec'),{'__builtins__':{'sum':sum}},scope)
        items.append(dict(kind='code',prompt=f'What integer does this Python program print? Return only the integer.\n{code}',answer=str(scope['result'])))
        candidates=[x for x in range(1,50) if x%7==a%7 and x%5==b%5]
        items.append(dict(kind='constraint',prompt=f'Return only the smallest positive integer less than 50 whose remainder modulo 7 is {a%7} and remainder modulo 5 is {b%5}.',answer=str(min(candidates))))
    return items


def corpus():
    data=dict(schema_version=1,training='The synthetic laboratory records a measured value, compares it with its earlier value, and checks the arithmetic. A changed claim should be checked against the stated conditions. ',heldout='A quiet valley lay beyond the river. The engineer checked each number before calculating the total. Clear instructions make a procedure easier to repeat. ',suite=suite())
    data['sha256']=digest(data);return data


def normalize(text):return ' '.join(text.casefold().strip().split())


def measure(engine,data,probe_tokens):
    import mlx.core as mx
    engine.reset('ordinary_state');engine.patch_config({'enabled':False,'read_only':True})
    ids=engine.tokenizer.encode(data['heldout'],add_special_tokens=False)
    losses=[];entropies=[]
    for i,token in enumerate(ids):
        logits=engine.consume(token)
        logp=logits-mx.logsumexp(logits);mx.eval(logp)
        if i+1<len(ids):losses.append(float(-logp[ids[i+1]].item()))
        entropies.append(float(-mx.sum(mx.exp(logp)*logp).item()))
    answers=[]
    for item in data['suite']:
        engine.reset('ordinary_state')
        text=engine.generate([dict(role='user',content=item['prompt'])],probe_tokens)['text']
        words=text.split();repetition=1-len(set(words))/len(words) if words else 0.
        answers.append(dict(**item,output=text,exact=normalize(text)==normalize(item['answer']),repetition=repetition))
    return dict(nll=float(np.mean(losses)),mean_entropy=float(np.mean(entropies)),accuracy=float(np.mean([a['exact'] for a in answers])),answers=answers)


def run(config,resume=False):
    from activation_field.admission import model_slot
    from activation_field.runtime import Engine
    from activation_field.config import FieldConfig
    from activation_field.manifest import read_manifest
    from activation_field.checkpoint import Vault
    from activation_field.measurements import field_drift
    root=Path(config['output']);data=corpus();manifest=read_manifest(config['manifest'])
    total=config.get('tokens',10000);stride=config.get('checkpoint_tokens',512)
    with run_lock(root):
        identity=prepare(root,config,data,manifest['baseline_hash'],resume)
        vault=Vault(root/'fields');result_path=root/'stability.json'
        result=json.loads(result_path.read_text()) if result_path.exists() else dict(status='running',checkpoint_tokens=[],identity=identity)
        done=result.get('consumed_tokens',0)
        def progress(state,**extra):atomic_json(root/'progress.json',dict(status=state,kind='stability',consumed_tokens=done,target_tokens=total,updated_at=time.time(),**extra))
        try:
            if 'baseline' not in result:
                progress('waiting_for_model',phase='baseline')
                with model_slot():
                    engine=Engine(manifest,FieldConfig(spectral_enabled=False))
                    try:result['baseline']=measure(engine,data,config.get('probe_tokens',32))
                    finally:engine.close()
                atomic_json(result_path,result)
            while done<total:
                progress('waiting_for_model',phase='exposure')
                with model_slot():
                    engine=Engine(manifest,FieldConfig(enabled=True,seed=config.get('seed',42),spectral_enabled=config.get('spectral',True)))
                    try:
                        if done:engine.restore(vault,result['last_checkpoint'])
                        ids=engine.tokenizer.encode(data['training'],add_special_tokens=False)
                        stop=min(total,((done//stride)+1)*stride)
                        for n in range(done,stop):
                            engine.consume(ids[n%len(ids)],'user')
                            if (n+1)%32==0:progress('running',phase='exposure',inflight_tokens=n+1)
                        engine.field.commit();name='boundary-'+str(stop)+'-'+uuid.uuid4().hex
                        # Unindexed checkpoints from crashes remain intact.
                        meta,tensors=engine.state();vault.save(name,meta,tensors);tensors=None
                        done=stop;result.update(consumed_tokens=done,last_checkpoint=name)
                        result['checkpoint_tokens'].append(done)
                        result['diagnostics']=engine.diagnostics();atomic_json(result_path,result)
                        if done==total:
                            progress('running',phase='final_measurement')
                            result['drift']=field_drift(engine.field)
                            result['post']=measure(engine,data,config.get('probe_tokens',32))
                    finally:engine.close()
                atomic_json(result_path,result)
            if 'post' not in result:
                with model_slot():
                    engine=Engine(manifest)
                    try:
                        engine.restore(vault,result['last_checkpoint']);result['drift']=field_drift(engine.field)
                        result['post']=measure(engine,data,config.get('probe_tokens',32))
                    finally:engine.close()
            base,post=result['baseline'],result['post']
            result.update(status='complete',nll_relative_increase=post['nll']/base['nll']-1,accuracy_change=post['accuracy']-base['accuracy'],meets_duration=done>=10000)
            result['stability_target_met']=done>=10000 and result['nll_relative_increase']<=.05 and result['accuracy_change']>=-.05
            result['scientific_memory_claim']='not_assessed_by_stability'
            atomic_json(result_path,result);progress('complete');return str(root)
        except Exception as exc:
            progress('interrupted',error=type(exc).__name__+': '+str(exc),last_checkpoint=result.get('last_checkpoint'));raise
