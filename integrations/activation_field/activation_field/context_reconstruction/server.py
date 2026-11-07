"""Authenticated loopback service; existing field and transform agents stay live."""
import hmac
from pathlib import Path
from fastapi import FastAPI,Depends,Header,HTTPException
from fastapi.responses import JSONResponse
from .jobs import ContextJobs

def app(root):
    root=Path(root);token=(root/'api-token').read_text().strip()
    def auth(authorization:str=Header(default='')):
        if not hmac.compare_digest(authorization,'Bearer '+token):raise HTTPException(401,'Unauthorized')
    api=FastAPI(title='Weight Changes to Context',docs_url=None,redoc_url=None,dependencies=[Depends(auth)])
    jobs=ContextJobs(root,root/'model-manifest.json');jobs.reconcile()
    @api.exception_handler(ValueError)
    @api.exception_handler(KeyError)
    async def invalid(request,exc):return JSONResponse(status_code=400,content=dict(error=dict(code=getattr(exc,'code','invalid_request'),message=str(exc),retryable=False)))
    @api.exception_handler(FileNotFoundError)
    async def missing(request,exc):return JSONResponse(status_code=404,content=dict(error=dict(code='not_found',retryable=False)))
    @api.get('/health')
    def health():return dict(ready=True,version='afctx/1',model_loaded=False,admission='shared model/image FIFO')
    @api.post('/context/start')
    def start(body:dict):return jobs.start('reconstruct',body)
    @api.get('/context/jobs/{run_id}')
    def status(run_id:str):
        jobs.reconcile();return jobs.get(run_id)
    @api.post('/context/jobs/{run_id}/cancel')
    def cancel(run_id:str,body:dict):return jobs.cancel(run_id,body)
    @api.post('/context/export')
    def export(body:dict):return jobs.export(body)
    return api

if __name__=='__main__':
    import uvicorn
    uvicorn.run(app(Path.home()/'.local/share/biome-activation-field'),host='127.0.0.1',port=8794)
