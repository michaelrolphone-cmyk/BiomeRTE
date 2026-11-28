"""Resumable full-model trials. A trial restarts from baseline if interrupted."""
import json
import time
import uuid
from pathlib import Path
from .corpus import corpus
from .storage import atomic_json, prepare, run_lock
from .report import report

CONDITIONS = ['C0','C1','C2','C3','C4','C5','C6','C7-8','C7-32','transcript']


def validate_config(config):
    kind=config.get('kind','memory')
    if kind not in ('memory','stability','descriptors'):raise ValueError('Unknown experiment kind')
    allowed = {'kind','manifest','output','corpus_seed'} | {
        'memory':{'episodes','conditions','retention_gaps','probe_tokens','seed','seeds','spectral'},
        'stability':{'tokens','checkpoint_tokens','probe_tokens','seed','spectral'},
        'descriptors':{'examples_per_template','persistent_homology'},
    }[kind]
    if set(config) - allowed:
        raise ValueError('Unknown experiment config field')
    conditions = config.get('conditions', CONDITIONS)
    if not isinstance(conditions, list) or not conditions or len(set(conditions)) != len(conditions) or any(c not in CONDITIONS for c in conditions):
        raise ValueError('Invalid conditions')
    seeds = config.get('seeds', [config.get('seed',42)])
    gaps = config.get('retention_gaps', [0,512,2048,8192])
    for values, minimum in ((seeds,0),(gaps,0)):
        if not isinstance(values,list) or not values or any(type(v) is not int or v < minimum for v in values) or len(set(values)) != len(values):
            raise ValueError('Seeds and gaps must be distinct nonnegative integers')
    for key, default, minimum in [('episodes',200,2),('probe_tokens',32,1),('corpus_seed',42,0),('tokens',10000,1),('checkpoint_tokens',512,1),('examples_per_template',5,1)]:
        v = config.get(key,default)
        if type(v) is not int or v < minimum:
            raise ValueError('Invalid '+key)
    for key in ('spectral','persistent_homology'):
        if type(config.get(key,False)) is not bool:raise ValueError(key+' must be boolean')
    if any(v>=2**32 for v in seeds):raise ValueError('Seeds must be uint32')


def run(config, resume=False):
    validate_config(config)
    if config.get('kind')=='stability':
        from .stability import run as stability
        return stability(config,resume)
    if config.get('kind')=='descriptors':
        from .descriptors import run as descriptors
        return descriptors(config,resume)
    with run_lock(config['output']):
        return _run(config, resume)


def _run(config, resume):
    from activation_field.admission import model_slot
    from activation_field.config import FieldConfig
    from activation_field.manifest import read_manifest
    from activation_field.checkpoint import Vault
    from activation_field.runtime import Engine
    import mlx.core as mx
    root = Path(config['output'])
    data = corpus(config.get('corpus_seed',42),config.get('episodes',200))
    manifest = read_manifest(config['manifest'])
    identity = prepare(root,config,data,manifest['baseline_hash'],resume)
    vault = Vault(root/'fields')
    path = root/'results.json'
    results = json.loads(path.read_text()) if path.exists() else []
    key = lambda r: (r.get('condition'),r.get('seed'),r.get('episode'),r.get('gap'))
    complete = {key(r) for r in results if r.get('status')=='complete'}
    conditions = config.get('conditions',CONDITIONS)
    seeds = config.get('seeds',[config.get('seed',42)])
    gaps = config.get('retention_gaps',[0,512,2048,8192])
    expected = len(conditions)*len(seeds)*len(data['episodes'])*len(gaps)
    def progress(state, **extra):
        atomic_json(root/'progress.json',dict(status=state,completed_trials=len(complete),expected_trials=expected,
                    identity=identity,updated_at=time.time(),**extra))
    progress('running')
    try:
        for condition in conditions:
            if condition in ('C3','C4'):
                if not any(r['condition']==condition and r['status']=='unexecuted' for r in results):
                    results.append(dict(condition=condition,status='unexecuted',reason='Exact matched synthetic write replay is not implemented; no unmatched substitute.'))
                    atomic_json(path,results)
                continue
            for seed in seeds:
                for episode in data['episodes']:
                    for gap in gaps:
                        trial = (condition,seed,episode['id'],gap)
                        if trial in complete:
                            continue
                        progress('waiting_for_model',trial=list(trial))
                        cfg = FieldConfig(enabled=condition not in ('C0','C1','transcript'),seed=seed,spectral_enabled=config.get('spectral',True))
                        if condition=='C5': cfg.role_scales['assistant']=0
                        if condition.startswith('C7-'): cfg.commit_every_tokens=int(condition.split('-')[1])
                        answers=[]; started=time.monotonic(); checkpoint=None
                        with model_slot():
                            engine=Engine(manifest,cfg)
                            try:
                                progress('running',trial=list(trial),phase='training')
                                if condition=='C0': engine.field.capture=False
                                def consume(token,role='user'):
                                    if condition=='C0': mx.eval(engine.model(mx.array([[token]]),cache=engine.cache))
                                    else: engine.consume(token,role)
                                for segment in episode['training_stream']:
                                    for token in engine.tokenizer.encode(segment['content'],add_special_tokens=False):
                                        consume(token,segment['role'])
                                neutral=engine.tokenizer.encode(data['unrelated_stream'],add_special_tokens=False)
                                for n in range(gap):
                                    consume(neutral[n%len(neutral)])
                                    if (n+1)%512==0: progress('running',trial=list(trial),phase='retention',retention_tokens=n+1)
                                if condition=='C6': engine.reset('weights')
                                engine.field.capture=True
                                engine.reset('ordinary_state')
                                engine.patch_config({'read_only':True,'enabled':False})
                                checkpoint='trial-'+uuid.uuid4().hex
                                meta,tensors=engine.state(field_only=True)
                                vault.save(checkpoint,meta,tensors); tensors=None
                                for index,probe in enumerate(episode['probes']):
                                    engine.restore(vault,checkpoint,reset_native_state=True)
                                    prompt=probe['prompt']
                                    if condition=='transcript': prompt=episode['training']+'\n'+prompt
                                    answer=engine.generate([dict(role='user',content=prompt)],config.get('probe_tokens',32))['text']
                                    answers.append(dict(**probe,output=answer,exact=' '.join(answer.casefold().split())==' '.join(probe['answer'].casefold().split())))
                                    progress('running',trial=list(trial),phase='probing',probes_complete=index+1)
                                results.append(dict(condition=condition,seed=seed,episode=episode['id'],gap=gap,split=episode['split'],status='complete',answers=answers,
                                                    accuracy=sum(a['exact'] for a in answers)/len(answers),seconds=time.monotonic()-started,
                                                    diagnostics=engine.diagnostics(),corpus_hash=data['sha256'],baseline_hash=manifest['baseline_hash']))
                                atomic_json(path,results); complete.add(trial)
                            finally:
                                engine.close()
                                if checkpoint: vault.delete(checkpoint)
        progress('complete' if len(complete)==expected else 'partial',next_action='Review report and unexecuted controls')
        report(root,root/'report')
        return str(root)
    except Exception as exc:
        progress('interrupted',error=type(exc).__name__+': '+str(exc),next_action='Resume with identical config; unfinished trial restarts independently from baseline')
        raise
