"""Authenticated HTTP route integration with a deterministic CPU job worker."""
import threading,time
from unittest.mock import patch
from fastapi.testclient import TestClient
from activation_field.server import app
from activation_field.experiment_jobs import ExperimentJobs


def test_durable_experiment_http_contract(tmp_path):
    with patch('activation_field.session.read_manifest',return_value={'baseline_hash':'fixture'}):
        api=app(tmp_path,'unused')
    client=TestClient(api)
    headers={'Authorization':'Bearer '+(tmp_path/'api-token').read_text().strip()}
    body=dict(request_id='new',expected_version=0,config={'episodes':2})
    assert client.post('/experiments',json=body).status_code==401
    with patch.object(ExperimentJobs,'_run',return_value=None):
        first=client.post('/experiments',json=body,headers=headers)
        assert first.status_code==200
        receipt=first.json()
        assert client.post('/experiments',json=body,headers=headers).json()==receipt
        assert client.post('/experiments',json=dict(body,config={'episodes':4}),headers=headers).status_code==400
        assert client.post('/experiments',json=dict(body,request_id='invalid',config=[]),headers=headers).status_code==400
    response=client.get('/experiments/'+receipt['run_id'],headers=headers)
    assert response.status_code==200 and response.json()['status']=='queued'
    assert client.get('/experiments/missing',headers=headers).status_code==404
    # A new process must expose interruption, not implicitly replay the job.
    with patch('activation_field.session.read_manifest',return_value={'baseline_hash':'fixture'}):
        second=TestClient(app(tmp_path,'unused'))
    state=second.get('/experiments/'+receipt['run_id'],headers=headers).json()
    assert state['status']=='interrupted' and state['version']==1
    with patch.object(ExperimentJobs,'_run',return_value=None):
        resume=dict(request_id='resume',run_id=receipt['run_id'],expected_version=1)
        assert second.post('/experiments',json=dict(resume,expected_version=True),headers=headers).status_code==400
        assert second.post('/experiments',json=resume,headers=headers).json()['version']==2
