"""Authenticated routes mounted into the existing field service."""
from .jobs import TransformJobs
from .common import TransformError


def install(api, root, manifest, sessions=None):
    from fastapi.responses import JSONResponse
    jobs=TransformJobs(root,manifest);jobs.reconcile()

    @api.exception_handler(TransformError)
    async def failure(request,exc):
        return JSONResponse(status_code=400,content=dict(error=dict(code=exc.code,message=str(exc),retryable=False)))

    @api.post('/transforms/fit')
    def fit(body:dict):return jobs.start('fit',body)
    @api.post('/transforms/materialize')
    def materialize(body:dict):return jobs.start('materialize',body)
    @api.post('/transforms/compare')
    def compare(body:dict):return jobs.start('compare',body)
    @api.post('/transforms/inspect')
    def inspect(body:dict):return jobs.inspect(body)
    @api.post('/transforms/export')
    def export(body:dict):return jobs.export(body)
    @api.get('/transforms/jobs/{run_id}')
    def status(run_id:str,details:bool=False):
        jobs.reconcile();return jobs.get(run_id,details=details)
    @api.post('/transforms/jobs/{run_id}/cancel')
    def cancel(run_id:str,body:dict):return jobs.cancel(run_id,body)
    @api.post('/transforms/jobs/{run_id}/assessment')
    def assessment(run_id:str,body:dict):
        return jobs.assess(run_id,body)
    @api.post('/transforms/jobs/{run_id}/trial')
    def trial(run_id:str,body:dict):
        if sessions is None:raise ValueError('Session manager unavailable')
        return jobs.trial(run_id,body,sessions)
    return jobs
