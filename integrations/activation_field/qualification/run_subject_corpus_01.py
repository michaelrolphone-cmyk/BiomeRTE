"""Resume corpus01 through normal Biome capabilities; preserve raw output for topic review."""
import base64
import datetime
import fcntl
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path.home()/'.local/share/biome-activation-field'
SIDE = Path.home()/'.local/share/biome-context-reconstruction'
installed = json.loads((SIDE/'source-version.json').read_text())
sys.path.insert(0, installed['source'])
from activation_field.provider import gateway
from activation_field.checkpoint import Vault, canonical
from activation_field.context_reconstruction.jobs import ContextJobs
from activation_field.context_reconstruction.transport import read_transfer
from activation_field.context_reconstruction.common import atomic_bytes

CONTEXT_RESOURCE = 'model.context-reconstruction.mac.a0e62492364bd9ddd423'
fixture = json.loads(Path(sys.argv[1]).read_text())
MODE = fixture['reconstruction']['options'].get('mode','first_pass')
PREFIX = 'afctx-corpus01-'+MODE.replace('_','-')
out = ROOT/('subject-corpus-01-'+MODE.replace('_','-')+'.json')
g = gateway()


def run():
    evidence = json.loads(out.read_text()) if out.exists() else dict(
        fixture_id=fixture['id'], corpus_sha256=hashlib.sha256(fixture['text'].encode()).hexdigest(),
        corpus_words=fixture['word_count'], installed_source=installed, checks={}, stages={})
    expected = hashlib.sha256(fixture['text'].encode()).hexdigest()
    if evidence['corpus_sha256'] != expected:
        raise ValueError('Corpus changed; a new experiment identity is required')

    def save(**fields):
        evidence.update(fields, observed_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
        atomic_bytes(out, json.dumps(evidence, indent=2).encode())

    def check(name, condition):
        evidence['checks'][name] = bool(condition)
        save()
        if not condition:
            raise AssertionError(name)

    def wait(operation):
        while True:
            receipt = g.invoke('cap.submission', dict(operation=operation), realm='site')
            save(waiting_for=operation, capability_state=receipt.get('state'))
            if receipt.get('state') in ('succeeded','failed','cancelled','rejected','uncertain'):
                if receipt['state'] != 'succeeded':
                    raise RuntimeError(str(receipt))
                value = receipt['result']['result']
                if not value.get('ok'):
                    raise RuntimeError(str(value))
                if 'result_artifact' in value:
                    raw = read_transfer(value['result_artifact'], lambda sha: base64.b64decode(
                        g.invoke('artifact.get', dict(sha256=sha), realm='site')['data']))
                    value = json.loads(raw)
                return value
            time.sleep(5)

    def call(contract, resource, operation, inputs):
        g.invoke('cap.submit', dict(contract=contract,resource=resource,operation=operation,inputs=inputs),
                 rid=operation+'-submit',realm='site')
        return wait(operation)

    try:
        save(status='running', stage='ingestion')
        # This operation was submitted already. Do not create another source session.
        ingest = wait(fixture['generation']['operation'])
        evidence['stages']['ingest'] = ingest
        check('source_generation_succeeded', ingest.get('status') == 'succeeded')
        save(stage='checkpoint')
        checkpoint = call(fixture['generation']['contract'],fixture['generation']['resource'],
            'afctx-corpus01-checkpoint',dict(request='',context=dict(action='checkpoint',
                session_id=ingest['session_id'],expected_version=ingest['version'],field_only=True)))
        evidence['stages']['checkpoint'] = checkpoint
        source = checkpoint['response']
        check('source_checkpoint_valid', isinstance(source.get('checkpoint_id'),str)
              and isinstance(source.get('sha256'),str))
        vault = Vault(ROOT/'state')
        check('source_hash_before', hashlib.sha256(canonical(vault.manifest(source['checkpoint_id']))).hexdigest() == source['sha256'])
        save(stage='reconstruction')
        started = call('model.context_reconstruction.start:1',CONTEXT_RESOURCE,
            PREFIX+'-reconstruct-v1',dict(arguments=dict(target_checkpoint=source['checkpoint_id'],
                expected_version=0,purpose=fixture['reconstruction']['purpose'],
                options=fixture['reconstruction']['options'])))
        evidence['stages']['start'] = started
        save(run_id=started['run_id'])
        jobs = ContextJobs(ROOT,ROOT/'model-manifest.json',launch=False)
        try:
            while True:
                state = jobs.get(started['run_id'])
                save(job_status=state)
                if state['status'] not in ('queued','running'):
                    break
                time.sleep(5)
        finally:
            jobs.db.close()
        state = call('model.context_reconstruction.status:1',CONTEXT_RESOURCE,
            PREFIX+'-completed-status',dict(arguments=dict(run_id=started['run_id'])))
        check('reconstruction_succeeded', state['status'] == 'succeeded')
        save(stage='export')
        result = call('model.context_reconstruction.export:1',CONTEXT_RESOURCE,
            PREFIX+'-export-v1',dict(arguments=dict(run_id=started['run_id'],
                expected_version=state['version'],allow_plaintext=True)))
        report = result['artifact']
        evidence['report'] = report
        body = dict(report)
        integrity = body.pop('integrity')
        check('report_integrity', hashlib.sha256(canonical(body)).hexdigest() == integrity['sha256'])
        check('source_hash_after', hashlib.sha256(canonical(vault.manifest(source['checkpoint_id']))).hexdigest() == source['sha256'])
        check('source_not_modified', report['source_modified'] is False)
        check('no_source_history_inputs', all(report[k] is False for k in
              ('uses_source_transcript','uses_source_native_caches','uses_source_logits')))
        if MODE == 'first_pass':
            check('first_pass_without_replay', report['replay_evaluations'] == 0
                  and report['candidate_kind'] == 'ranked_token_hypotheses'
                  and report['sequence_order_inferred'] is False)
        else:
            check('full_model_replay', bool(report.get('candidates')) and all(
                c['scored_projections'] == 248 and c['scored_layers'] == 32 for c in report['candidates']))
        # Retain output unchanged. No overlap target, anti-copy rule or automatic semantic pass.
        save(status='awaiting_subject_review', stage='subject review',
             raw_reconstructed_text=report['estimated_context'],
             output_kind=report.get('candidate_kind'),token_order=report.get('token_order'),
             evaluation=fixture['evaluation'],
             semantic_verdict=None,
             note='Topic relevance, related concepts, coherence and drift require review of raw output.')
        print(json.dumps(dict(status=evidence['status'],path=str(out),
             raw_reconstructed_text=report['estimated_context'],
             best_relative_squared_error=report.get('best_relative_squared_error'))))
    except BaseException as exc:
        save(status='interrupted' if isinstance(exc,(KeyboardInterrupt,SystemExit)) else 'failed',
             error=type(exc).__name__+': '+str(exc))
        raise


with out.with_suffix('.lock').open('a+b') as lock:
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        print(json.dumps(dict(status='already_running',path=str(out))))
    else:
        run()
