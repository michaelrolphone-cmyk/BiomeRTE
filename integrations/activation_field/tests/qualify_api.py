"""Live authenticated API checks; synthetic inputs, no external LM Studio API."""
import json,threading,uuid,time
from urllib.request import urlopen,Request
from urllib.error import HTTPError
from activation_field.provider import api,ROOT

checks={};created=[]
def checked(name,value):
    checks[name]=bool(value)
    if not value:raise AssertionError(name)
def expect_http(code,call):
    try:call()
    except HTTPError as error:return error.code==code
    return False
def identity():return str(uuid.uuid4())
try:
    checked('authentication_required',expect_http(401,lambda:urlopen('http://127.0.0.1:8791/health')))
    body=dict(request_id=identity(),expected_version=0,persist=True)
    a=api('/sessions',body);created.append(a['session_id'])
    checked('create_retry_identical',api('/sessions',body)==a)
    b=api('/sessions',dict(request_id=identity(),expected_version=0,persist=True));created.append(b['session_id'])
    path='/sessions/'+a['session_id']
    change=dict(request_id=identity(),expected_version=0,config={'enabled':True})
    first=api(path+'/config',change,'PATCH')
    checked('config_retry_identical',api(path+'/config',change,'PATCH')==first)
    checked('changed_retry_rejected',expect_http(409,lambda:api(path+'/config',dict(change,config={'enabled':False}),'PATCH')))
    checked('stale_version_rejected',expect_http(409,lambda:api(path+'/config',dict(change,request_id=identity()),'PATCH')))
    checked('other_session_unchanged',api('/sessions/'+b['session_id'])['config']['enabled'] is False)
    rid=identity();events=[];failures=[];started=threading.Event()
    generate=dict(request_id=rid,expected_version=0,messages=[dict(role='user',content='Count from one to one hundred.')],max_tokens=100,stream=True)
    bpath='/sessions/'+b['session_id']
    def read():
        try:
            request=Request('http://127.0.0.1:8791'+bpath+'/generate',json.dumps(generate).encode(),{'Authorization':'Bearer '+(ROOT/'api-token').read_text().strip(),'Content-Type':'application/json'})
            with urlopen(request) as response:
                for raw in response:
                    if raw.startswith(b'data: '):events.append(json.loads(raw[6:]));started.set()
        except Exception as error:failures.append(str(error));started.set()
    worker=threading.Thread(target=read);worker.start();started.wait()
    if failures:raise RuntimeError(failures)
    state=api(bpath);cancel=dict(request_id=identity(),expected_version=state['version'])
    cancelled=api(bpath+'/requests/'+rid+'/cancel',cancel)
    checked('cancel_retry_identical',api(bpath+'/requests/'+rid+'/cancel',cancel)==cancelled)
    worker.join()
    if failures:raise RuntimeError(failures)
    receipt=api(bpath+'/requests/'+rid)
    checked('cancel_boundary_reported',receipt['status']=='succeeded' and receipt['result']['finish_reason']=='cancelled' and 'committed_token_count' in receipt)
    checked('stream_completion',any(e['type']=='complete' for e in events))
    prior_version=api(bpath)['version'];events.clear();read()
    checked('no_replay_on_same_request',not failures and api(bpath)['version']==prior_version and events[-1]['receipt']==receipt)
    result=dict(status='passed',checks=checks,cancel_receipt=receipt)
except Exception as error:
    result=dict(status='failed',checks=checks,error=str(error));raise
finally:
    for sid in created:
        state=api('/sessions/'+sid)
        if not state['busy']:api('/sessions/'+sid,dict(request_id=identity(),expected_version=state['version']),'DELETE')
    (ROOT/'api-qualification.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result))
