"""Loopback sidecar for additive deployment without restarting field sessions."""
import hmac
from pathlib import Path
from fastapi import FastAPI,Depends,Header,HTTPException
from fastapi.responses import JSONResponse
from ..checkpoint import Vault
from ..provider import api as field_api
from .service import install


def app(root):
    root=Path(root);token=(root/'api-token').read_text().strip()
    def auth(authorization:str=Header(default='')):
        if not hmac.compare_digest(authorization,'Bearer '+token):raise HTTPException(401,'Unauthorized')
    api=FastAPI(title='Field Transforms',docs_url=None,redoc_url=None,dependencies=[Depends(auth)])
    class Sessions:
        vault=Vault(root/'state')
        def open_trial(self,checkpoint,request_id):
            body=dict(request_id=request_id,expected_version=0,persist=True,
                      config=dict(enabled=False,read_only=True,baseline_pull=0.,spectral_enabled=False))
            if checkpoint:
                body.update(checkpoint_id=checkpoint,reset_native_state=True)
                return field_api('/sessions/import',body)
            return field_api('/sessions',body)
    jobs=install(api,root,root/'model-manifest.json',Sessions())
    @api.exception_handler(ValueError)
    @api.exception_handler(KeyError)
    async def invalid(request,exc):return JSONResponse(status_code=400,content=dict(error=dict(code='invalid_request',message=str(exc),retryable=False)))
    @api.exception_handler(FileNotFoundError)
    async def missing(request,exc):return JSONResponse(status_code=404,content=dict(error=dict(code='not_found',retryable=False)))
    @api.get('/health')
    def health():return dict(ready=True,version='afwt/1',model_loaded=False,admission='shared model/image FIFO')
    return api


if __name__=='__main__':
    import uvicorn
    uvicorn.run(app(Path.home()/'.local/share/biome-activation-field'),host='127.0.0.1',port=8793)
