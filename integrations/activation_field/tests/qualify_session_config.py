"""Live persisted-session regression: an explicit off toggle survives restore."""
import json,uuid
from activation_field.provider import api,ROOT

sid=None;checks={}
def rid():return str(uuid.uuid4())
def check(name,value):
    checks[name]=bool(value)
    if not value:raise AssertionError(name)
try:
    created=api('/sessions',dict(request_id=rid(),expected_version=0,persist=True,config={'enabled':True,'spectral_enabled':False}))
    sid=created['session_id'];path='/sessions/'+sid
    first=api(path+'/generate',dict(request_id=rid(),expected_version=0,messages=[{'role':'user','content':'Say hi.'}],max_tokens=1))
    check('enabled_request_succeeded',first['status']=='succeeded')
    before=first['result']['diagnostics'];check('enabled_request_changed_weights',before['changed_words_total']>0)
    changed=api(path+'/config',dict(request_id=rid(),expected_version=first['version'],config={'enabled':False,'read_only':True}),'PATCH')
    second=api(path+'/generate',dict(request_id=rid(),expected_version=changed['version'],messages=[{'role':'user','content':'Say hi.'},{'role':'assistant','content':first['result']['text']},{'role':'user','content':'Say yes.'}],max_tokens=1))
    check('restored_request_succeeded',second['status']=='succeeded')
    after=second['result']['diagnostics']
    check('off_toggle_survives_restore',after['field_enabled'] is False and after['read_only'] is True)
    check('off_toggle_preserves_weights',before['changed_words_total']==after['changed_words_total'] and before['field_version']==after['field_version'])
    check('restored_request_consumes_new_tokens',after['consumed_token_count']>before['consumed_token_count'])
    result=dict(status='passed',checks=checks,before=before,after=after)
except Exception as exc:
    result=dict(status='failed',checks=checks,error=type(exc).__name__+': '+str(exc));raise
finally:
    if sid:
        state=api('/sessions/'+sid)
        if not state['busy']:api('/sessions/'+sid,dict(request_id=rid(),expected_version=state['version']),'DELETE')
    (ROOT/'session-config-qualification.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(dict(status=result['status'],checks=checks,error=result.get('error'))))
