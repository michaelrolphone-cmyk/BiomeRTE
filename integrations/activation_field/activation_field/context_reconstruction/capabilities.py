"""Discoverable inverse reconstruction operations, no source transcript inputs."""
import sys,hashlib,json
NAMESPACE='model.context_reconstruction.'
OPERATIONS=('start','status','cancel','export')

def offers(gateway,resource,module='activation_field.context_reconstruction.provider'):
    descriptions={
        'start':'target_checkpoint, purpose, options (max_tokens, candidate_pool, beam_width, sketch_rank, power_iterations, vocabulary_batch, seed, role, layout). New jobs require expected_version=0. Resume with only resume_run_id and expected_version. Options are explicit search budgets; no runtime cap.',
        'status':'run_id. Reports state, progress and measured residual; no reconstructed text is disclosed.',
        'cancel':'run_id, expected_version. Cooperatively stop at a safe boundary; keep replay measurements for explicit resume.',
        'export':'run_id, expected_version, allow_plaintext=true. Explicitly disclose the protected context and estimated token stream. A large response is returned through result_artifact; recursively retrieve chunks/nodes with artifact.get and verify digests.'}
    result=[]
    for operation in OPERATIONS:
        name=NAMESPACE+operation
        usage=dict(before='Experimental delta-only input reconstruction on the native pinned model. Shared model/image admission. Does not modify the source or use its conversation, native caches or logits. Results are hypotheses, not recovered history.',
                   parameters={'arguments':descriptions[operation]+' Biome operation IDs provide idempotency.'},
                   read_result='Read result.ok and job status. Queue acceptance is not completion. Text stays encrypted locally until explicit export. Reconstruction does not reinject or reinforce the model.')
        revision=hashlib.sha256(json.dumps(usage,sort_keys=True).encode()).hexdigest()[:16]
        gateway.invoke('cap.define',dict(name=name,version=1,inputs={'arguments':'obj'},outputs={'result':'obj'},description='Weight changes to context: '+operation,usage=usage),rid='afctx-define-'+operation+'-'+revision)
        result.append(dict(contract=name+':1',resource=resource,service='activation-field',capacity=1,usage=usage,argv=[sys.executable,'-m',module,'adapter',operation]))
    return result

def bind_contract(payload,operation=None):
    if operation not in OPERATIONS:raise ValueError('Unknown configured operation')
    contract=NAMESPACE+operation+':1'
    if payload.get('contract',contract)!=contract:raise ValueError('Offer/contract mismatch')
    return dict(payload,contract=contract)

def adapter(payload,api):
    from .jobs import identifier
    from .transport import transport_result
    from urllib.error import HTTPError
    from .common import ContextError
    contract=payload['contract']
    if not contract.startswith(NAMESPACE) or not contract.endswith(':1'):raise ValueError('Unknown contract')
    action=contract[len(NAMESPACE):-2]
    if action not in OPERATIONS:raise ValueError('Unknown action')
    body=dict(payload['inputs']['arguments']);body.pop('request_id',None)
    try:
        if action=='status':
            if set(body)!={'run_id'}:raise ValueError('status requires only run_id')
            value=api('/context/jobs/'+identifier(body['run_id']))
        elif action=='cancel':
            run_id=identifier(body.pop('run_id'));body['request_id']=payload['operation']
            value=api('/context/jobs/'+run_id+'/cancel',body)
        else:
            if action=='start':body['request_id']=payload['operation']
            value=api('/context/'+action,body)
    except HTTPError as exc:
        try:error=json.loads(exc.read()).get('error',{})
        except (ValueError,TypeError):error={}
        raise ContextError(error.get('code','http_error'),error.get('message',str(exc))) from exc
    return transport_result(dict(ok=True,**value))
