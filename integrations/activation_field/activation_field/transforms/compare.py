"""Matched B/T/R trials. Empty caches and read-only state for every probe."""
import hashlib
import time
import numpy as np
from ..checkpoint import Vault
from .common import digest, TransformError, check_cancel
from .source import CheckpointReader
from .evaluate import paired_kl


def compare(engine, vault, target, restored, suite, output, resume=False, progress=None, cancel=None):
    if not isinstance(suite,dict) or not isinstance(suite.get('probes'),list) or not suite['probes']:
        raise ValueError('Comparison requires a nonempty probes list')
    for p in suite['probes']:
        if not isinstance(p.get('id'),str) or not isinstance(p.get('prompt'),str) or not p['prompt']:
            raise ValueError('Each probe requires id and prompt')
        if type(p.get('max_tokens',32)) is not int or p.get('max_tokens',32)<1:raise ValueError('Invalid probe token budget')
    ids=[p['id'] for p in suite['probes']]
    if len(set(ids))!=len(ids):raise ValueError('Duplicate probe ID')
    target_reader=CheckpointReader(vault,target);restored_reader=CheckpointReader(vault,restored)
    identity=digest(dict(base=engine.manifest['baseline_hash'],target=target_reader.hash,restored=restored_reader.hash,suite=suite))
    store=Vault(output)
    if (store.root/'comparison.enc').exists():
        report=store.load_json('comparison.enc')
        if not resume:raise FileExistsError('Use explicit comparison resume')
        if report['identity']!=identity:raise TransformError('source_version_changed','Comparison inputs changed')
    else:
        report=dict(identity=identity,source=dict(baseline=engine.manifest['baseline_hash'],target=target_reader.hash,restored=restored_reader.hash),
                    probe_suite_hash=digest(suite),conditions={},paired_kl={},writes=False,decay=False,initial_native_state='empty',
                    scientific_usefulness='unresolved; operator assessment required',assessment=None,selected_candidate=None)
    def prepare(checkpoint):
        engine.reset('entire_session')
        if checkpoint:engine.restore(vault,checkpoint,reset_native_state=True)
        engine.patch_config(dict(enabled=False,read_only=True,baseline_pull=0.,spectral_enabled=False))
        engine.reset('ordinary_state');engine.reset('activity_traces')
        engine.boundary_hook=lambda _:check_cancel(cancel)
    started=time.monotonic()
    # T first supplies the common teacher-forced distribution reference.
    for condition,checkpoint in [('T',target),('B',None),('R',restored)]:
        report['conditions'].setdefault(condition,{})
        for probe in suite['probes']:
            check_cancel(cancel)
            key=probe['id'];name=condition+'-'+hashlib.sha256(key.encode()).hexdigest()[:24]
            if key in report['conditions'][condition]:continue
            prepare(checkpoint)
            tokens=engine.tokenizer.encode(probe['prompt']+probe.get('continuation',''),add_special_tokens=False)
            if not tokens:raise ValueError('Probe has no tokens')
            logits=[]
            for token in tokens:
                check_cancel(cancel);logits.append(np.asarray(engine.consume(token),np.float32))
            logits=np.stack(logits)
            if condition=='T':
                if not (store.root/name).exists():store.save(name,dict(baseline_hash=engine.manifest['baseline_hash'],identity=identity),[('logits',logits)])
            else:
                reference=dict(store.arrays('T-'+hashlib.sha256(key.encode()).hexdigest()[:24]))['logits']
                report['paired_kl'].setdefault(key,{})['T_to_'+condition]=dict(mean=float(paired_kl(reference,logits).mean()),positions=len(tokens),same_prefixes=True)
            del logits
            # Free generation is a separate reset and never supplies prefixes to KL.
            prepare(checkpoint)
            result=engine.generate([dict(role='user',content=probe['prompt'])],max_tokens=probe.get('max_tokens',32),temperature=0.,cancel=cancel if hasattr(cancel,'is_set') else None)
            normalize=lambda v:' '.join(str(v).casefold().split())
            score=None if 'expected' not in probe else int(normalize(result['text'])==normalize(probe['expected']))
            report['conditions'][condition][key]=dict(text=result['text'],score=score,finish_reason=result['finish_reason'],kind=probe.get('kind','general'))
            store.save_json('comparison.enc',report)
            if progress:progress(dict(stage='compare',condition=condition,probe=key))
    raw={c:[p['score'] for p in v.values() if p['score'] is not None] for c,v in report['conditions'].items()}
    means={c:float(np.mean(v)) if v else None for c,v in raw.items()};report['scores']=means
    report['retention_ratio']=None
    if all(v is not None for v in means.values()) and means['T']!=means['B']:
        report['retention_ratio']=(means['R']-means['B'])/(means['T']-means['B'])
    scored=[p['id'] for p in suite['probes'] if 'expected' in p]
    report['paired_bootstrap_95']={}
    if scored:
        rng=np.random.default_rng(suite.get('bootstrap_seed',20261104))
        for a,b in [('R','B'),('R','T'),('T','B')]:
            diffs=np.array([report['conditions'][a][p]['score']-report['conditions'][b][p]['score'] for p in scored])
            samples=np.array([rng.choice(diffs,size=len(diffs),replace=True).mean() for _ in range(2000)])
            report['paired_bootstrap_95'][a+'-'+b]=np.quantile(samples,[.025,.975]).tolist()
    report['seconds']=time.monotonic()-started;report['status']='succeeded'
    report['uncertainty_scope']='paired probe bootstrap; synthetic smoke probes are not held-out memory acceptance'
    target_reader.verify();restored_reader.verify();store.save_json('comparison.enc',report)
    return report
