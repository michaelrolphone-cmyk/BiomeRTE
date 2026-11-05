import hmac,secrets
from pathlib import Path
from fastapi import FastAPI,Header,HTTPException,Depends
from fastapi.responses import StreamingResponse
from .session import Sessions,Conflict


def app(root,manifest,initial_config=None):
    root=Path(root);root.mkdir(parents=True,exist_ok=True,mode=0o700)
    tokenfile=root/'api-token'
    if not tokenfile.exists():
        with tokenfile.open('x') as f:tokenfile.chmod(0o600);f.write(secrets.token_urlsafe(48))
    token=tokenfile.read_text().strip();sessions=Sessions(root,manifest)
    def auth(authorization:str=Header(default='')):
        if not hmac.compare_digest(authorization,'Bearer '+token):raise HTTPException(401,'Unauthorized')
    api=FastAPI(title='Activation Field',docs_url=None,redoc_url=None,dependencies=[Depends(auth)])
    from .experiment_jobs import ExperimentJobs
    experiments=ExperimentJobs(root,manifest)
    from .transforms.service import install as install_transforms
    install_transforms(api,root,manifest,sessions)
    @api.post('/experiments')
    def experiment_create(body:dict):return experiments.start(body)
    @api.get('/experiments/{run_id}')
    def experiment_status(run_id:str):return experiments.get(run_id)
    @api.exception_handler(Conflict)
    async def conflict(request,exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=409,content={'error':{'code':'conflict','message':str(exc),'retryable':False}})
    @api.exception_handler(ValueError)
    @api.exception_handler(KeyError)
    async def invalid(request,exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=400,content={'error':{'code':'invalid_request','message':str(exc),'retryable':False}})
    @api.exception_handler(FileNotFoundError)
    async def missing(request,exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404,content={'error':{'code':'not_found','retryable':False}})
    @api.get('/health')
    def health():return dict(ready=True,version='0.1.0',model_loaded=sessions.active is not None,active_session=sessions.active,admission=sessions.admission,baseline_hash=sessions.manifest['baseline_hash'],field_default=False,baseline_pull_default=0)
    @api.post('/sessions')
    def create(body:dict):
        body.setdefault('config',initial_config or {})
        return sessions.create(body)
    @api.get('/sessions/{sid}')
    def get(sid:str):
        s=sessions.get(sid);return {k:s[k] for k in ['session_id','version','config','baseline_hash','committed_tokens','busy']}
    @api.post('/sessions/{sid}/generate')
    def generate(sid:str,body:dict):
        sessions.start(sid,body)
        if body.get('stream',False):return StreamingResponse(sessions.stream(sid,body['request_id']),media_type='text/event-stream')
        import json
        for event in sessions.stream(sid,body['request_id']):
            for line in event.splitlines():
                if line.startswith('data: '):
                    value=json.loads(line[6:])
                    if value.get('type')=='complete':return value['receipt']
    @api.get('/sessions/{sid}/requests/{rid}')
    def status(sid:str,rid:str):return sessions.receipt(sid,rid)
    @api.get('/sessions/{sid}/requests/{rid}/events')
    def events(sid:str,rid:str,after:int=0):return StreamingResponse(sessions.stream(sid,rid,after),media_type='text/event-stream')
    @api.post('/sessions/{sid}/requests/{rid}/cancel')
    def cancel(sid:str,rid:str,body:dict):return sessions.cancel_request(sid,rid,body)
    @api.patch('/sessions/{sid}/config')
    def config(sid:str,body:dict):return sessions.configure(sid,body)
    @api.get('/sessions/{sid}/diagnostics')
    def diagnostics(sid:str):return sessions.get(sid).get('diagnostics',{'state':'not_loaded'})
    @api.post('/sessions/{sid}/reset')
    def reset(sid:str,body:dict):return sessions.mutate(sid,'reset',body)
    @api.post('/sessions/{sid}/checkpoint')
    def checkpoint(sid:str,body:dict):return sessions.mutate(sid,'checkpoint',body)
    @api.post('/sessions/{sid}/fork')
    def fork(sid:str,body:dict):return sessions.mutate(sid,'fork',body)
    @api.delete('/sessions/{sid}')
    def delete(sid:str,body:dict):return sessions.mutate(sid,'delete',body)
    @api.post('/sessions/import')
    def imported(body:dict):
        meta=sessions.vault.manifest(body['checkpoint_id'])['metadata']
        if meta['baseline_hash']!=sessions.manifest['baseline_hash']:raise ValueError('Baseline mismatch')
        body.setdefault('config',meta['config'])
        created=sessions.create(body);s=sessions.get(created['session_id'])
        if s.get('checkpoint'):return created
        # Import copies are independent; native state always reset by default.
        import uuid
        name='import-'+uuid.uuid4().hex
        sessions.vault.save(name,meta,sessions.vault.arrays(body['checkpoint_id']))
        s['checkpoint']=name;s['reset_native_on_load']=body.get('reset_native_state',True);sessions.save(s)
        return created
    return api
