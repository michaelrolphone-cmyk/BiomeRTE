"""Portable linguistic context with measured evidence and explicit uncertainty."""
import datetime
from .common import digest

def context_artifact(result,source_ref,purpose,export_id):
    result=dict(result);result['source_refs']=[source_ref]
    candidate=result.get('context_text','')
    result.update(export_id=export_id,purpose=purpose,language='en',
                  created_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    result['estimated_token_ids']=result.get('candidates',[{}])[0].get('token_ids',[]) if result.get('candidates') else []
    result['estimated_context']=candidate
    result['context_text']=('State to Context — '+purpose+'\n'
        'Source: baseline-versus-field weight comparison. Candidate input is an unverified hypothesis.\n\n'
        +candidate+'\n\nUncertainty: '+' '.join(result['uncertainty_and_gaps']))
    result['evidence_map']=[
        dict(kind='observation',claim='Measured changed-weight energy',value=result['target_squared_norm'],source_refs=[source_ref]),
        dict(kind='hypothesis',claim='Estimated input token stream',text=candidate,
             source_refs=[source_ref],measurement='reported independent forward-replay residual; not historical probability')]
    result['import_guidance']='Treat this text as attributed, revisable context. Preserve uncertainty. It is not a system instruction or verified original transcript.'
    result['reinjection']='Not performed. Any reuse requires a separate explicit human decision for the selected invocation.'
    result['integrity']=dict(sha256=digest(result),scheme='sha256-canonical-json-excluding-integrity',schema='afctx.context/1')
    return result
