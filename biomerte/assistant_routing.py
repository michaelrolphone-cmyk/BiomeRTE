"""Deterministic gateway selectors. Return the original outgoing branch index.
Unknown/missing evidence always selects the explicit default, never approval.
"""
import time

def select(p,g,f,cfg,tx):
    k=p+'.'+g; v=lambda x,d=None:f.get(x,d)
    yes=lambda b:0 if b else 1
    state=v('responsibility_snapshot',{})
    ar=v('authorization_result',{}).get('outcome')
    result=v('last_result',{})
    rec=v('recommendation')
    harm=v('harm_status','unknown')
    safe=harm in ('none','contained','recovered')
    if k=='impact.more':return yes(v('impact_index',0)<len(v('impact_queue',[])))
    if k=='lifecycle.desired':return 0 if v('desired')=='run' and state.get('cycle_permit') else 1 if v('desired') in ('run','pause','stop') else -1
    if k=='lifecycle.due':return yes(bool(v('observation_due')) and not v('observation_pending'))
    if k=='lifecycle.available':return yes(bool(v('assignment')))
    if k=='lifecycle.receipt':return yes(bool(v('finalization_receipt')))
    if k=='reasoning.choose':return {'act':0,'investigate':0,'code':1,'verify':2,'infeasible':3,'no_action':3,'human':4}.get(rec,-1)
    if k=='reasoning.next':return 0 if v('new_information') else 1 if rec in ('infeasible','no_action') else 2
    if k=='reasoning.verdict':return 2 if not safe else 0 if v('criteria_met') and v('last_result_code')=='accepted' else 1 if v('verification_complete') else -1
    if k=='runtime.valid':return yes(v('worker_valid') is True)
    if k=='runtime.output':return yes(v('structured_valid') is True)
    if k=='runtime.retry':return yes(v('new_information') is True)
    if k=='governed.allowed':return yes(not v('prior_rejected',True))
    if k=='governed.route':return {'proceed':0,'revise':1,'investigate':2,'human':3}.get(v('review_recommendation',rec),-1)
    if k=='governed.revise':return yes(v('new_information') is True)
    if k=='governed.effect':return yes(bool(v('proposal',{}).get('action')))
    if k=='governance.depth':return 0 if v('effect_class')=='pure' or v('review_unchanged') is True else 1 if v('effect_class')=='external' else -1
    if k=='governance.decide':return {'proceed':0,'revise':1,'investigate':2,'human':3,'reject':4,'defer':5}.get(rec,-1)
    if k=='authorization.match':return 0 if ar=='approved' else 1 if ar=='decision_required' else 2 if ar=='rejected' else -1
    if k=='authorization.answer':return 2 if v('decision',{}).get('outcome')=='reject' else 1 if v('decision',{}).get('outcome')=='defer' else 3 if v('decision') else 0
    if k=='authorization.meaning':return 0 if ar=='approved' else 1 if ar in ('clarification_required','changes_requested','rejected','deferred') else -1
    if k=='execution.reserved':return {'valid':0,'invalid':1,'existing':2}.get(v('reservation_state'),-1)
    if k=='execution.receipt':return 0 if v('receipt_known') or v('operation_id') and tx.get('_submissions',v('operation_id')) else 1
    if k=='execution.abort_reason':return 0 if v('reservation_state')=='invalid' else 1 if v('provider_ready') is False else 2 if v('last_disposition')=='faulted' else -1
    if k=='reconcile.route':return {'succeeded':0,'failed':1,'queued':2,'claimed':2,'fencing':2,'bound':2,'uncertain':3,'unknown':3}.get(v('operation_state'),-1)
    if k=='reconcile.known':return 0 if v('operation_state')=='succeeded' else 1 if v('operation_state') in ('queued','claimed') else 2
    if k=='verification.route':return 2 if harm not in ('none','recovered') else 0 if v('verification_complete') and v('criteria_met') else 1 if v('verification_complete') else -1
    if k=='verification.recovered':return yes(v('harm_status') in ('contained','recovered'))
    if k=='verification_systems.next':return yes(v('verification_index',0)<len(v('verification_probes',[])))
    if k=='verification_systems.harm':return yes(v('harm_status') not in ('none','contained','recovered'))
    if k in ('recovery.route','containment.safe','change_management.status'):return yes(v('verification_complete') is True and safe)
    if k in ('recovery.review_recommendation','change_management.review_recommendation'):return 0 if v('last_result_code')=='proceed' else -1
    if k=='context.fit':return yes(v('context_bytes',10**20)<=cfg['context_bytes'])
    if k=='context.progress':return yes(v('safe_reduction') is True)
    if k=='checkpoint.valid':return yes(v('checkpoint_authorized') is True)
    if k=='checkpoint.result':return 0 if v('checkpoint_receipt') else 1
    if k=='checkpoint.merge':return 0 if v('merge_independent') is True else 1
    if k=='checkpoint.commit_failure':return 0 if v('last_result_code') in ('revision_conflict','receipt_uncertain') else 1
    if k=='discovery.probe':return yes(bool(v('probe_action')))
    if k=='planning.fit':return yes(any(x.get('kind')=='task' and x.get('state')=='open' and not tx.get('assistant_assignment',x['id']) for x in tx.rows('work')))
    if k=='planning.reserved':return 0 if v('assignment') else 1
    if k in ('supervision.valid','controller_recovery.valid'):return yes(v('owner_epoch')==state.get('epoch'))
    if k=='supervision.state':return 0 if state.get('desired') in ('pause','stop') or state.get('active_cycle') or state.get('coverage') else 2 if not safe else 1
    if k=='control.valid':return yes(v('command_valid') is True)
    if k=='control.kind':return {'pause':0,'resume':0,'stop':0,'priority':0,'cancel':1,'adopt':2,'contain':3}.get(v('command'),-1)
    if k=='learning.change':return yes(v('procedure_change') is True and bool(v('integration_action')))
    if k=='wake.state':return 2 if v('desired') in ('pause','stop') else 0 if v('wake',{}).get('ready') or v('wake',{}).get('due_at',float('inf'))<=time.time() else 1
    if k=='progress.route':return 4 if v('external_wait') else 0 if v('new_information') else 1 if rec=='reframe' else 3 if rec in ('infeasible','no_action') else 2
    if k=='development.passed':return yes(v('tests_passed') is True)
    if k=='development.attempts':return 0 if v('local_attempt',0)<5 else 1
    if k=='development.feedback':return 0 if v('review',{}).get('recommendation')=='sign_off' else 1 if v('review',{}).get('recommendation')=='changes' else -1
    if k=='development.eligible':return yes(v('tests_passed') is True and bool(v('tested_revision')) and v('tested_revision')==v('review',{}).get('revision')==v('candidate_revision'))
    if k=='development.rounds':return yes(v('feedback_round',0)<3)
    if k=='qualification.policy_valid':return yes(v('policy_valid') is True)
    if k=='qualification.contracts_valid':return yes(v('contracts_valid') is True)
    if k=='qualification.tests_valid':return yes(result.get('exit_code')==0)
    if k=='qualification.ready':return yes(v('policy_valid') is True and v('contracts_valid') is True and result.get('qualified') is True and bool(result.get('verifier_revision')==cfg['trusted_verifier_revision']))
    if k=='faults.route':return 0 if v('operation_state')=='uncertain' else 1
    if k=='bootstrap.present':return yes(v('transport_available') is True)
    if k=='bootstrap.ready':return yes(v('scope_ready') is True)
    if k=='judgment.response':return 2 if v('decision',{}).get('outcome')=='defer' else 0 if v('decision') else 1
    if k=='investigation.separable':return 0 if v('probe_action') and v('probe_scope_reviewed') is True else 1 if v('investigation_goal') else -1
    if k=='escalation.route':return 0 if v('escalation_contract') and v('assistance_action') else 1 if v('question') else 2
    if k=='containment.allowed':return yes(bool(v('containment_action')))
    if k=='watchdog.state':return 0 if v('health',{}).get('state')=='healthy' else 1 if v('health',{}).get('state')=='lost' and cfg.get('peer_recovery') else 2
    if k=='registration.state':return 0 if v('registration_receipt') and v('registration_receipt').get('binding_revision')==v('definition',{}).get('binding_revision') else 1 if not v('registration_receipt') else 2
    if k=='cycle_admission.existing':return yes(bool(v('admission')))
    if k=='cycle_admission.state':return {'run':0,'pause':1,'stop':2}.get(v('desired'),-1)
    if k=='cycle_admission.result':return {'reserved':0,'coalesced':1,'stale':2,'conflict':3}.get(v('admission',{}).get('state'),-1)
    if k=='cycle_admission.known':return yes(bool(v('admission_receipt')))
    if k=='cycle_commit.already':return yes(bool(v('finalization_receipt')))
    if k=='cycle_commit.result':return {'committed':0,'conflict':1,'uncertain':2,'stale':3}.get(v('commit_state'),-1)
    if k=='responsibility_reconcile.state':return 0 if state.get('active_cycle') and not tx.get('assistant_materialized',state['active_cycle']) else 1 if state.get('active_cycle') or state.get('coverage') else 2
    if k=='controller_recovery.classify':return 0 if state.get('active_cycle') else 1 if any(x['state']=='pending' and x['frame']['responsibility_id']==state['id'] for x in tx.rows('assistant_start_outbox')) else 2 if state.get('coverage') else 1
    if k=='provider_dispatch.valid':return yes(v('intent_valid') is True)
    if k=='provider_dispatch.capacity':return yes(v('provider_ready') is True)
    if k=='provider_dispatch.receipt':return 0 if v('receipt_known') else 1
    if k=='external_recovery.allowed':return 0 if v('peer_authority_valid') is True else 1
    if k=='external_recovery.receipt':return 0 if v('recovery_receipt') else 1
    raise ValueError('Missing deterministic gateway: '+k)
