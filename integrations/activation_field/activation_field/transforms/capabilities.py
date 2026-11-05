"""Distinct discoverable capabilities sharing the field provider and resource."""
import sys
import hashlib
import json

NAMESPACE='model.field_transform.'
OPERATIONS=('fit','inspect','materialize','compare','export','status','cancel','assessment','trial')


def offers(gateway,resource,module='activation_field.provider'):
    result=[]
    parameters={
        'fit':'target_checkpoint (committed field checkpoint ID), config (optional fitting config). Returns durable run_id immediately. Resume uses resume_run_id and current expected_version.',
        'inspect':'run_id of fit and candidate_id. Returns actual encoded size, SHA256, coverage and provenance.',
        'materialize':'fit_run_id and candidate_id. Creates an independent encrypted checkpoint from the pinned baseline; original target is not read. Returns durable run_id.',
        'compare':'target_checkpoint, restored_checkpoint, suite.probes [{id,prompt,continuation?,expected?,kind?,max_tokens?}]. Matched read-only B/T/R, fresh native state per probe. Returns durable run_id.',
        'export':'run_id and candidate_id. Explicitly exports the complete AFWT as a checksum-verified artifact tree. Read artifact.root with artifact.get, recursively follow nodes/parts, concatenate chunk bytes and verify artifact.sha256/bytes. Treat output as conversation-derived data.',
        'status':'run_id; optional details=true returns the complete measured report including B/T/R free-generation observations. Returns progress, costs, partial candidates and explicit failure state.',
        'cancel':'run_id and expected_version. Cooperative cancellation retains progress at a safe boundary; no source writes.',
        'assessment':'run_id of completed comparison, expected_version, observations, attribution, selected_candidate?, blind_labels?. Records caller-attributed subjective feedback.',
        'trial':'run_id of completed comparison, expected_version, blind=false. Creates independent read-only Baseline/Evolved/Restored sessions, optionally A/B/C. Switch by passing the chosen session_id and version to agent.activation_field.delegate:1.'}
    for operation in OPERATIONS:
        name=NAMESPACE+operation
        usage=dict(before='Native Activation Field extension. Full pinned baseline, same model/image admission. 1 MB is advisory; no default region count or runtime cap.',
                   parameters={'arguments':parameters[operation]+' Mutations use the Biome operation ID for idempotency; new jobs require expected_version=0.'},
                   read_result='Check result.ok. A queued/running job is not completed; read model.field_transform.status:1 with its run_id. Completed materialize results provide checkpoint_id for independent read-only session import. Large reports return result_artifact with result_encoding=biome-artifact-json/1; retrieve its complete JSON through artifact.get using the same checksum-verified tree protocol as export. No artifact size cap is imposed.')
        revision=hashlib.sha256(json.dumps(usage,sort_keys=True).encode()).hexdigest()[:16]
        gateway.invoke('cap.define',dict(name=name,version=1,inputs={'arguments':'obj'},outputs={'result':'obj'},description='Activation Field functional transform: '+operation,usage=usage),rid='afwt-define-'+operation+'-'+revision)
        result.append(dict(contract=name+':1',resource=resource,service='activation-field',capacity=1,usage=usage,
                           argv=[sys.executable,'-m',module,'adapter',operation]))
    return result


def bind_contract(payload,operation=None):
    # The generic worker deliberately sends operation ID and inputs only.
    # Bind the route using the trusted per-offer argv, not user arguments.
    if operation is None:return payload
    if operation not in OPERATIONS:raise ValueError('Unknown configured operation')
    contract=NAMESPACE+operation+':1'
    if payload.get('contract',contract)!=contract:raise ValueError('Offer/contract mismatch')
    return dict(payload,contract=contract)


def adapter(payload,api):
    contract=payload.get('contract','')
    if not contract.startswith(NAMESPACE):return None
    action=contract[len(NAMESPACE):].split(':')[0]
    if action not in OPERATIONS:raise ValueError('Unknown transform action')
    body=dict(payload['inputs']['arguments']);body.pop('request_id',None)
    if action=='status':
        from .jobs import identifier
        value=api('/transforms/jobs/'+identifier(body['run_id'])+('?details=true' if body.get('details',False) else ''))
    elif action in ('cancel','assessment','trial'):
        from .jobs import identifier
        run_id=identifier(body.pop('run_id'));body['request_id']=payload['operation']
        value=api('/transforms/jobs/'+run_id+'/'+action,body)
    else:
        if action in ('fit','materialize','compare'):body['request_id']=payload['operation']
        value=api('/transforms/'+action,body)
    from .transport import transport_result
    return transport_result(dict(ok=True,**value))
