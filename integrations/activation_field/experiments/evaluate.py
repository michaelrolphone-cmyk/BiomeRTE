"""Resumable full-model trials; unexecuted controls are never reported passed."""
import json,time,uuid
from pathlib import Path
from activation_field.admission import model_slot
from activation_field.config import FieldConfig
from activation_field.manifest import read_manifest
from activation_field.checkpoint import Vault
from .corpus import corpus


def run(config,resume=False):
    from activation_field.runtime import Engine
    root=Path(config['output']);root.mkdir(parents=True,exist_ok=True)
    corpus_path=root/'corpus.json'
    if corpus_path.exists():data=json.loads(corpus_path.read_text())
    else:
        data=corpus(config.get('corpus_seed',42),config.get('episodes',200));corpus_path.write_text(json.dumps(data,indent=2))
    (root/'config.json').write_text(json.dumps(config,indent=2))
    manifest=read_manifest(config['manifest']);vault=Vault(root/'fields')
    report_path=root/'results.json';results=json.loads(report_path.read_text()) if resume and report_path.exists() else []
    complete={(r.get('condition'),r.get('episode'),r.get('gap')) for r in results if r.get('status')=='complete'}
    conditions=config.get('conditions',['C0','C1','C2','C3','C4','C5','C6','C7-8','C7-32'])
    for condition in conditions:
        if condition in ('C3','C4'):
            # These controls cannot be approximated and described as matched.
            results.append(dict(condition=condition,status='unexecuted',reason='Exact synthetic perturbation-replay corpus required; never substitute an unmatched control.'))
            report_path.write_text(json.dumps(results,indent=2));continue
        for episode in data['episodes']:
            for gap in config.get('retention_gaps',[0,512,2048,8192]):
                if (condition,episode['id'],gap) in complete:continue
                cfg=FieldConfig(enabled=condition not in ('C0','C1'),seed=config.get('seed',42),spectral_enabled=config.get('spectral',True))
                if condition=='C5':cfg.role_scales['assistant']=0
                if condition.startswith('C7-'):cfg.commit_every_tokens=int(condition.split('-')[1])
                answers=[];start=time.monotonic()
                with model_slot():
                    engine=Engine(manifest,cfg)
                    try:
                        if condition=='C0':engine.field.capture=False
                        # C0 explicitly bypasses all field observation wrappers.
                        if condition=='C0':
                            import mlx.core as mx
                            ids=engine.tokenizer.encode(episode['training'],add_special_tokens=False)
                            for token in ids:mx.eval(engine.model(mx.array([[token]]),cache=engine.cache))
                        else:engine.ingest_text(episode['training'])
                        if gap:
                            neutral=engine.tokenizer.encode('An unrelated synthetic sequence. ',add_special_tokens=False)
                            for n in range(gap):
                                if condition=='C0':mx.eval(engine.model(mx.array([[neutral[n%len(neutral)]]]),cache=engine.cache))
                                else:engine.consume(neutral[n%len(neutral)],'user')
                        if condition=='C6':engine.reset('weights')
                        engine.field.capture=True;engine.reset('ordinary_state');engine.patch_config({'read_only':True,'enabled':False})
                        name='trial-'+uuid.uuid4().hex;meta,tensors=engine.state(field_only=True);vault.save(name,meta,tensors);tensors=None
                        for probe in episode['probes']:
                            engine.restore(vault,name,reset_native_state=True)
                            answer=engine.generate([{'role':'user','content':probe['prompt']}],config.get('probe_tokens',32))['text']
                            answers.append(dict(**probe,output=answer,exact=answer.strip().casefold()==probe['answer'].casefold()))
                        result=dict(condition=condition,episode=episode['id'],gap=gap,split=episode['split'],status='complete',answers=answers,accuracy=sum(a['exact'] for a in answers)/len(answers),seconds=time.monotonic()-start,diagnostics=engine.diagnostics(),corpus_hash=data['sha256'])
                        results.append(result);report_path.write_text(json.dumps(results,indent=2))
                        vault.delete(name)
                    finally:engine.close()
    return str(root)


def report(run_dir,output):
    root=Path(run_dir);dest=Path(output);dest.mkdir(parents=True,exist_ok=True)
    rows=json.loads((root/'results.json').read_text());conditions={}
    for r in rows:
        bucket=conditions.setdefault(r['condition'],dict(complete=0,accuracy_sum=0,unexecuted=[]))
        if r['status']=='complete':bucket['complete']+=1;bucket['accuracy_sum']+=r['accuracy']
        else:bucket['unexecuted'].append(r.get('reason','unexecuted'))
    for b in conditions.values():b['accuracy']=b['accuracy_sum']/b['complete'] if b['complete'] else None
    summary=dict(conditions=conditions,scientific_success='not_established',reason='Targets require held-out paired confidence intervals and exact matched controls; a smoke test is not evidence of useful memory.')
    (dest/'report.json').write_text(json.dumps(summary,indent=2));return summary
