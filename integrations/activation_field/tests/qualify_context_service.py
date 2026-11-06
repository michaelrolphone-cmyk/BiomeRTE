"""Prepare synthetic native checkpoints and test the complete protected job runner.

Run through the Biome terminal capability. No source prompt is passed to the
reconstructor. The expected text remains solely in this qualification harness.
"""
import datetime,hashlib,json,time,sys
from activation_field.context_reconstruction.transport import read_transfer
from pathlib import Path
from activation_field.checkpoint import Vault,canonical
from activation_field.config import FieldConfig
from activation_field.manifest import read_manifest
from activation_field.runtime import Engine
from activation_field.admission import model_slot
from activation_field.context_reconstruction.jobs import ContextJobs
from experiments.storage import source_identity

def run():
    root=Path.home()/'.local/share/biome-activation-field'
    out=root/'context-service-qualification.json'
    from activation_field.provider import gateway
    gateway=gateway()
    identity=json.loads((Path.home()/'.local/share/biome-context-reconstruction/identity.json').read_text())
    resource='model.context-reconstruction.mac.'+identity['node'][:20]
    def capability(action,args,operation):
        gateway.invoke('cap.submit',dict(contract='model.context_reconstruction.'+action+':1',resource=resource,operation=operation,inputs={'arguments':args}),rid=operation,realm='site')
        while True:
            receipt=gateway.invoke('cap.submission',dict(operation=operation),realm='site')
            if receipt['state'] in ('succeeded','failed','cancelled','rejected'):break
            time.sleep(2)
        if receipt['state']!='succeeded':raise RuntimeError(str(receipt))
        value=receipt['result']['result']
        if not value['ok']:raise RuntimeError(str(value))
        if 'result_artifact' in value:
            import base64
            value=json.loads(read_transfer(value['result_artifact'],lambda sha:base64.b64decode(gateway.invoke('artifact.get',dict(sha256=sha),realm='site')['data'])))
        return value
    evidence=dict(status='running',source_identity=source_identity(),checks={},cases={},
                  observed_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    def publish():
        evidence['observed_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
        tmp=out.with_suffix('.tmp');tmp.write_text(json.dumps(evidence,indent=2));tmp.replace(out)
    def check(name,value):
        evidence['checks'][name]=bool(value);publish()
        if not value:raise AssertionError(name)
    publish();vault=Vault(root/'state');expected={}
    try:
        with model_slot(lambda info:(evidence.update(stage='prepare',progress=info),publish())):
            e=Engine(read_manifest(root/'model-manifest.json'),FieldConfig(enabled=True,spectral_enabled=False))
            try:
                for label,text in [('single',' jade'),('sequence',' jade cedar'),('zero','')]:
                    e.reset('entire_session');ids=e.tokenizer.encode(text,add_special_tokens=False)
                    for token in ids:e.consume(token)
                    name='afctx-qualification-'+label
                    meta,tensors=e.state(field_only=True)
                    if not (vault.root/name).exists():vault.save(name,meta,tensors)
                    tensors=None
                    expected[label]=ids
                    evidence['cases'][label]=dict(checkpoint_id=name,expected_tokens=ids,
                         source_hash=hashlib.sha256(canonical(vault.manifest(name))).hexdigest())
            finally:e.close()
        jobs=ContextJobs(root,root/'model-manifest.json',launch=False)
        for label in ('single','sequence','zero'):
            case=evidence['cases'][label]
            options=dict(mode='replay',max_tokens=max(1,len(expected[label])),candidate_pool=4,beam_width=2,sketch_rank=2,power_iterations=4)
            operation='afctx-native-'+label+'-'+source_identity()[:16]
            receipt=capability('start',dict(expected_version=0,target_checkpoint=case['checkpoint_id'],purpose='native synthetic qualification',options=options),operation)
            case['capability_operation']=operation
            case.update(run_id=receipt['run_id']);evidence.update(stage=label);publish()
            while True:
                status=jobs.get(receipt['run_id']);case['status']=status;publish()
                if status['status'] not in ('queued','running'):break
                time.sleep(2)
            status=capability('status',dict(run_id=receipt['run_id']),operation+'-status')
            case['status']=status;publish()
            check(label+'_job_succeeded',status['status']=='succeeded')
            report=capability('export',dict(run_id=receipt['run_id'],expected_version=status['version'],allow_plaintext=True),operation+'-export')['artifact']
            case['report']=report;publish()
            check(label+'_source_unchanged',case['source_hash']==hashlib.sha256(canonical(vault.manifest(case['checkpoint_id']))).hexdigest())
            content=dict(report);integrity=content.pop('integrity')
            check(label+'_integrity',integrity['sha256']==hashlib.sha256(canonical(content)).hexdigest())
            if label=='zero':
                check('zero_signal_no_invented_tokens',report['status']=='insufficient_signal' and not report['estimated_token_ids'])
            else:
                check(label+'_full_model_replay',all(c['scored_projections']==248 and c['scored_layers']==32 for c in report['candidates']))
                check(label+'_no_history_used',all(report[k] is False for k in ('uses_source_transcript','uses_source_native_caches','uses_source_logits')))
                check(label+'_source_is_hypothesis',report['candidate_kind']=='hypothesis')
                if label=='single':
                    check('single_token_recovered',report['estimated_token_ids']==expected[label])
                    check('single_replay_exact',report['best_relative_squared_error']==0)
                else:
                    case['exact_sequence_recovered']=report['estimated_token_ids']==expected[label]
                    case['scope']='Exploratory multi-token accuracy; not an acceptance assertion.'
                publish()
        evidence['status']='passed';publish()
    except BaseException as exc:
        evidence.update(status='failed',error=type(exc).__name__+': '+str(exc));publish();raise
    print(json.dumps(dict(status=evidence['status'],checks=evidence['checks'],path=str(out))))

if __name__=='__main__':run()
