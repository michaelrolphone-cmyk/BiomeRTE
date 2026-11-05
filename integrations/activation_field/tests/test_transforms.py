import dataclasses
import io
import json
import math
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pytest
from activation_field.checkpoint import Vault
from activation_field.transforms.common import TransformError,digest
from activation_field.transforms.regions import Region,evaluate_delta,evaluate_transform
from activation_field.transforms.format import encode,decode,save_transform,load_transform,inspect_transform
from activation_field.transforms.coordinates import pack,unpack,quantize,split_ids
from activation_field.transforms.fit import configuration,fit
from activation_field.transforms.source import RuntimeSource
from activation_field.transforms.materialize import materialize
from activation_field.transforms.evaluate import paired_kl


class FixtureSource:
    def __init__(self, target=True, shape=(32,64)):
        self.target='synthetic' if target else None
        self.tile_bytes=4*1024*1024
        self.baseline_ref=dict(baseline_hash='a'*64)
        d=dict(id=0,path='layer.weight',logical_shape=list(shape),orientation='row-column',dtype='uint32',frozen=False,
               s_m=1.,quantization_ref=dict(bits=4,group_size=64))
        self.descriptors=[d];self.stats={}
        base=np.full(shape,8,np.uint8);self.base=base
        row,col=np.arange(shape[0])[:,None],np.arange(shape[1])[None,:]
        r=Region(0,0,.5,.5,math.log(.4),math.log(.4),.3,4.)
        floating=evaluate_delta([r],d,row,col,base.astype(float))
        self.target_codes=np.clip(np.rint(base+floating),0,15).astype(np.uint8) if target else base.copy()
        self.identity=dict(baseline_ref=self.baseline_ref,target_hash=digest(self.target_codes.tolist()),field_version=1)
        self.engine=SimpleNamespace(state=lambda field_only:(dict(baseline_hash='a'*64,tensor_map_version=1,kind='field_only',
            config=dict(window_tokens=128,stride_tokens=32),field_version=0),[]))
    def verify(self):pass
    def tiles(self,d):
        for start in range(0,len(self.base),7):
            b=self.base[start:start+7];t=self.target_codes[start:start+7]
            yield dict(start=start,base=b.astype(float),target=t.astype(float),base_codes=b,target_codes=t,scale=np.ones_like(b,float),offset=np.zeros_like(b,float))
    def characterize(self,progress=None):
        d=self.descriptors[0];d['s_m']=8.
        changed=int(np.count_nonzero(self.base!=self.target_codes));self.stats[0]=dict(count=self.base.size,changed=changed,unchanged=self.base.size-changed)
        return self.stats
    manifest=RuntimeSource.manifest


def artifact(regions=None,goal=1000000):
    source=FixtureSource();source.characterize()
    return source.manifest(configuration(dict(target_bytes=goal))), regions or []


def test_formula_signed_rotated_additive_and_tensor_isolation():
    d=artifact()[0]['tensor_map'][0];d['s_m']=1.
    r=Region(1,0,.5,.5,math.log(.2),math.log(.4),0.,-.04)
    # Cell centers: .6 versus center .5 yields d=.5 on first radius.
    value=evaluate_delta([r],d,.6*32-.5,.5*64-.5,.2)
    assert value==pytest.approx(-.02)
    assert evaluate_delta([r,r],d,15.5,31.5,.2)==pytest.approx(-.08)
    assert evaluate_delta([r],dict(d,id=1),15.5,31.5,.2)==0
    rotated=dataclasses.replace(r,angle=math.pi/2)
    assert evaluate_delta([rotated],d,.5*32-.5,.6*64-.5,.2)==pytest.approx(-.02)
    assert evaluate_delta([r],d,.9*32-.5,31.5,.2)==0


def test_value_window_and_pure_scalar():
    m,_=artifact();d=m['tensor_map'][0]
    r=Region(0,0,.5,.5,0.,0.,0.,.5,.2,1.,2.,2,1)
    assert evaluate_transform((m,[r]),0,(15.5,31.5),8.)==12.
    assert evaluate_transform((m,[r]),0,(15.5,31.5),24.)==24.


def test_deterministic_format_size_advisory_and_fresh_process(tmp_path):
    m,r=artifact([Region(0,0,.5,.5,-1.,-2.,.3,.4)],goal=1)
    data=encode(m,r);assert data==encode(m,r)
    path=tmp_path/'context.afwt';path.write_bytes(data)
    receipt=inspect_transform(path);assert receipt['above_goal'] and receipt['parameter_bytes']==48
    assert receipt['bytes']==receipt['parameter_bytes']+receipt['manifest_bytes']+receipt['container_bytes']
    code='from activation_field.transforms.format import load_transform;from activation_field.transforms.regions import evaluate_transform;import sys;print(evaluate_transform(load_transform(sys.argv[1]),0,(15,31),8.))'
    result=subprocess.check_output([sys.executable,'-c',code,str(path)],text=True)
    assert float(result)==evaluate_transform(load_transform(path),0,(15,31),8.)
    assert zipfile.ZipFile(io.BytesIO(data)).namelist()==['manifest.json','regions.bin']


@pytest.mark.parametrize('patch',[{'kernel':1},{'reserved':1},{'hv':0,'window':1},{'log_h0':-1000},{'a':float('nan')},{'exponent':3}])
def test_reject_invalid_parameters(patch):
    m,r=artifact([dataclasses.replace(Region(0,0,.5,.5,0.,0.,0.,1.),**patch)])
    with pytest.raises(TransformError):encode(m,r)


def test_corrupt_binary_and_extra_members_rejected():
    m,r=artifact([Region(0,0,.5,.5,0.,0.,0.,1.)]);data=encode(m,r)
    z=zipfile.ZipFile(io.BytesIO(data));out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as other:
        other.writestr('manifest.json',z.read('manifest.json'))
        other.writestr('regions.bin',z.read('regions.bin')[:-1])
    with pytest.raises(TransformError):decode(out.getvalue())
    out=io.BytesIO(data)
    with zipfile.ZipFile(out,'a') as other:other.writestr('residual.bin',b'hidden')
    with pytest.raises(TransformError):decode(out.getvalue())


def test_quantization_ties_clipping_zero_scale_and_preservation():
    b=np.array([[0,1,2,3,14,15,7,8]],np.uint8)
    t=dict(base_codes=b,base=b.astype(float),scale=np.array([[1,1,1,1,1,1,0,1.]]),offset=np.zeros_like(b,float))
    delta=np.array([[.5,.5,-.5,-.5,10,10,50,0.]])
    q,audit=quantize(t,delta)
    np.testing.assert_array_equal(q,[[0,2,2,2,15,15,7,8]])
    assert audit['clipped']==2 and audit['zero_scale']==1
    np.testing.assert_array_equal(unpack(pack(q)),q)
    np.testing.assert_array_equal(pack(quantize(t,np.zeros_like(delta))[0]),pack(b))


def test_split_independent_of_tiles():
    whole=split_ids('x',(32,64),0,32,5,[.8,.1,.1])
    tiled=np.concatenate([split_ids('x',(32,64),i,min(7,32-i),5,[.8,.1,.1]) for i in range(0,32,7)])
    np.testing.assert_array_equal(whole,tiled)


def test_identity_and_restoration_resume(tmp_path):
    source=FixtureSource(False);source.characterize()
    m=source.manifest(configuration());path=tmp_path/'identity.afwt';save_transform(m,[],path)
    vault=Vault(tmp_path/'checkpoints')
    receipt=materialize(source,path,vault,'restored',tmp_path/'staging')
    assert receipt['native_state']=='empty'
    assert all(not k.endswith('.codes') for k,a in vault.arrays('restored'))
    meta=vault.manifest('restored')['metadata']
    assert meta['config']['read_only'] and not meta['config']['enabled']
    again=materialize(source,path,vault,'restored',tmp_path/'staging',resume=True)
    assert again['sha256']==receipt['sha256']


def test_fit_signed_surface_candidate_and_resume_source_guard(tmp_path):
    source=FixtureSource()
    config=dict(value_modes=['disabled'],falloff_exponents=[1],target_bytes=100,convergence_relative_improvement=.01)
    result=fit(source,tmp_path/'fit',config)
    assert len(result['candidates'])>1
    assert result['report']['global_metrics']['changed_target_code_agreement']>.5
    assert all(r['above_goal'] for r in result['candidates'])
    assert result['candidates'][-1]['validation_loss']<result['candidates'][0]['validation_loss']
    candidate=tmp_path/'fit'/(result['candidates'][-1]['candidate_id']+'.afwt.enc')
    assert not candidate.read_bytes().startswith(b'PK')
    assert inspect_transform(candidate)['sha256']==result['candidates'][-1]['sha256']
    source.identity['field_version']=2
    with pytest.raises(TransformError,match='changed'):fit(source,tmp_path/'fit',config,resume=True)
    # A fragment committed before its top-level journal still carries identity.
    (tmp_path/'fit/private/fit.enc').unlink()
    with pytest.raises(TransformError,match='changed'):fit(source,tmp_path/'fit',config)


def test_cancel_then_resume_materialization(tmp_path):
    source=FixtureSource(False);source.characterize();m=source.manifest(configuration())
    path=tmp_path/'f.afwt';save_transform(m,[Region(0,0,.5,.5,-.5,-.5,0.,.2)],path)
    calls=[0]
    def cancel():
        calls[0]+=1
        if calls[0]>2:raise TransformError('cancelled','pause')
    vault=Vault(tmp_path/'v')
    with pytest.raises(TransformError):materialize(source,path,vault,'result',tmp_path/'stage',cancel=cancel)
    assert not (vault.root/'result').exists()
    result=materialize(source,path,vault,'result',tmp_path/'stage',resume=True)
    assert result['checkpoint_id']=='result'


def test_stable_kl_and_capability_routing():
    from activation_field.transforms.capabilities import adapter
    x=np.array([[1000.,999.,-1000.]])
    np.testing.assert_allclose(paired_kl(x,x),[0])
    assert paired_kl(x,x[:,::-1])[0]>0
    seen=[]
    result=adapter(dict(contract='model.field_transform.fit:1',operation='op1',inputs={'arguments':{'expected_version':0,'target_checkpoint':'target'}}),lambda path,body:seen.append((path,body)) or {'run_id':'abc'})
    assert result['ok'] and seen[0][1]['request_id']=='op1'


def test_generic_worker_route_is_bound_by_offer_argv():
    from activation_field.transforms.capabilities import offers,bind_contract,adapter
    class Gateway:
        def invoke(self,*args,**kwargs):pass
    for offer in offers(Gateway(),'resource','activation_field.transforms.provider'):
        operation=offer['argv'][-1]
        payload=bind_contract(dict(operation='op',inputs={'arguments':{'run_id':'run'}}),operation)
        assert payload['contract']==offer['contract']
    payload=bind_contract(dict(operation='status-op',inputs={'arguments':{'run_id':'run'}}),'status')
    result=adapter(payload,lambda path:dict(run_id='run',status='running',result=None))
    assert isinstance(result,dict) and result['ok']
    with pytest.raises(ValueError):bind_contract(dict(contract='model.field_transform.fit:1'),'status')


def test_export_transfer_exceeds_size_goal_and_reassembles_nested_parts():
    import base64,hashlib
    from activation_field.transforms.transport import transport_result,read_transfer,FRAME_BYTES,FANOUT
    store={};sizes=[]
    def put(data):
        sha=hashlib.sha256(data).hexdigest();store[sha]=data;sizes.append(len(data))
        return dict(sha256=sha,size=len(data))
    data=np.random.default_rng(7).bytes(FRAME_BYTES*(FANOUT+1)+13)
    value=dict(ok=True,filename='native.afwt',encoding='base64',data=base64.b64encode(data).decode(),
               bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
    result=transport_result(value,put)
    assert result['encoding']=='biome-artifact-tree/1' and 'data' not in result
    assert max(sizes)<=FRAME_BYTES and len(json.dumps(result).encode())<FRAME_BYTES
    assert read_transfer(result['artifact'],store.__getitem__)==data
    assert transport_result(value,put)==result
    root=result['artifact']['root'];store[root['sha256']]+=b'corrupt'
    with pytest.raises(TransformError):read_transfer(result['artifact'],store.__getitem__)


def test_large_status_report_preserves_full_data_and_inline_version():
    import hashlib
    from activation_field.transforms.transport import transport_result,read_transfer
    store={}
    def put(data):
        sha=hashlib.sha256(data).hexdigest();store[sha]=data;return dict(sha256=sha,size=len(data))
    value=dict(ok=True,run_id='run',version=7,status='running',result={'candidates':[{'id':i,'details':'x'*100} for i in range(500)]})
    result=transport_result(value,put)
    assert result['status']=='running' and result['version']==7
    assert json.loads(read_transfer(result['result_artifact'],store.__getitem__))==value
    small=dict(ok=True,run_id='run',version=7,status='queued')
    assert transport_result(small)==small


def test_transport_rejects_incorrect_storage_receipts():
    from activation_field.transforms.transport import publish_bytes
    with pytest.raises(TransformError):publish_bytes(b'abc',lambda data:dict(sha256='wrong',size=len(data)),'application/x-afwt')


def test_capability_preserves_typed_service_errors():
    from urllib.error import HTTPError
    from activation_field.transforms.capabilities import adapter
    def api(*args):
        raise HTTPError('http://localhost/transforms/materialize',400,'Bad Request',{},
                        io.BytesIO(b'{"error":{"code":"baseline_mismatch","message":"Pinned baseline differs"}}'))
    with pytest.raises(TransformError) as error:
        adapter(dict(contract='model.field_transform.materialize:1',operation='op',inputs={'arguments':{}}),api)
    assert error.value.code=='baseline_mismatch' and str(error.value)=='Pinned baseline differs'


def test_centered_solver_matches_weighted_svd():
    from activation_field.transforms.fit import fit_coefficients,training_problem
    rng=np.random.default_rng(61);d=artifact()[0]['tensor_map'][0]
    a=np.column_stack([rng.integers(0,32,4000),rng.integers(0,64,4000),rng.normal(0,3,4000),rng.normal(0,.2,4000),rng.integers(0,2,4000),np.zeros(4000),rng.uniform(.2,1,4000)])
    r=Region(0,0,.5,.4,-.5,-1.,.4,.1,.2,.3,2.,2,1)
    problem=training_problem([],d,a,configuration());x,y,u,residual,weights=problem
    phi,psi=r.basis(x,y,u);matrix=np.column_stack([phi,psi])*np.sqrt(weights)[:,None]
    expected=np.linalg.lstsq(matrix,residual*np.sqrt(weights),rcond=None)[0]
    fitted=fit_coefficients(r,[],d,a,configuration(),True,problem)
    np.testing.assert_allclose([fitted.a,fitted.b],expected,rtol=1e-10,atol=1e-12)


def test_joint_overlap_solver_direct_and_streamed_agree():
    from activation_field.transforms.fit import refit_neighborhood,sample_loss
    rng=np.random.default_rng(90);d=artifact()[0]['tensor_map'][0]
    rows=rng.integers(0,32,3000);cols=rng.integers(0,64,3000);values=rng.normal(0,2,3000)
    truth=[Region(0,0,.45,.5,-.7,-.8,.1,.3,.07),Region(1,0,.55,.5,-.7,-.8,.1,-.2,.04)]
    target=evaluate_delta(truth,d,rows,cols,values)
    a=np.column_stack([rows,cols,values,target,np.ones(3000),np.zeros(3000),np.ones(3000)])
    initial=[dataclasses.replace(r,a=r.a/2,b=r.b/2) for r in truth]
    for workspace in (1000000,4096):
        fitted=refit_neighborhood(initial,d,a,configuration(),workspace)
        assert sample_loss(fitted,d,a,0,configuration())<1e-20


def test_durable_jobs_versions_replay_encryption_and_recovery(tmp_path,monkeypatch):
    import activation_field.transforms.jobs as module
    monkeypatch.setattr(module,'read_manifest',lambda *args,**kw:dict(baseline_hash='a'*64))
    Vault(tmp_path/'state').save('target',dict(baseline_hash='a'*64),[])
    jobs=module.TransformJobs(tmp_path,'manifest',launch=False)
    body=dict(request_id='fit1',expected_version=0,target_checkpoint='target')
    receipt=jobs.start('fit',body);run=receipt['run_id']
    assert jobs.start('fit',body)==receipt
    with pytest.raises(ValueError):jobs.start('fit',dict(body,target_checkpoint='another'))
    assert jobs._row(run)['config']=='encrypted'
    assert jobs.configuration(jobs._row(run))['target_checkpoint']=='target'
    with pytest.raises(ValueError):jobs.cancel(run,dict(request_id='bad',expected_version=99))
    cancel=dict(request_id='cancel',expected_version=0)
    assert jobs.cancel(run,cancel)==jobs.cancel(run,cancel)
    jobs.reconcile();assert jobs.get(run)['status']=='interrupted'
    candidate=dict(candidate_id='partial',bytes=100,sha256='b'*64,region_count=1,validation_loss=.5,above_goal=False)
    Vault(jobs.jobs/run/'fit/private').save_json('fit.enc',dict(candidates=[candidate],tensor_index=2,regions=['private parameters']))
    exposed=jobs.get(run)['result']
    assert exposed['partial'] and exposed['candidates']==[candidate] and 'regions' not in exposed
    with pytest.raises(ValueError):jobs.start('fit',dict(request_id='badresume',expected_version=0,resume_run_id=run))
    version=jobs.get(run)['version']
    resumed=jobs.start('fit',dict(request_id='resume',expected_version=version,resume_run_id=run))
    assert resumed['version']==version+1 and not jobs._row(run)['cancel']


def test_live_worker_lock_and_assessment_guards(tmp_path):
    import fcntl
    from activation_field.transforms.jobs import TransformJobs
    jobs=TransformJobs(tmp_path,'manifest',launch=False)
    with jobs.db:jobs.db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,NULL,NULL,0)',('run',0,'running','compare','encrypted'))
    folder=jobs.jobs/'run';folder.mkdir()
    with (folder/'worker.lock').open('a+b') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        jobs.reconcile();assert jobs.get('run')['status']=='running'
    jobs.reconcile();assert jobs.get('run')['status']=='interrupted'
    jobs._finish('run','succeeded');version=jobs.get('run')['version']
    Vault(folder/'result').save_json('result.enc',{})
    body=dict(request_id='assessment',expected_version=version,observations='No memory claim from this smoke test',attribution='test operator')
    assert jobs.assess('run',body)==jobs.assess('run',body)
    with pytest.raises(ValueError):jobs.assess('run',dict(body,request_id='stale'))


def test_native_bfloat16_quantization_metadata(tmp_path):
    mx=pytest.importorskip('mlx.core')
    weights=mx.array(pack(np.full((2,64),8,np.uint8)))
    module=SimpleNamespace(weight=weights,scales=mx.full((2,1),.25,dtype=mx.bfloat16),biases=mx.full((2,1),-1.,dtype=mx.bfloat16))
    engine=SimpleNamespace(manifest=dict(baseline_hash='a'*64,repository='fixture',revision='b'*40),field=SimpleNamespace(entries=[('layer',module)],anchor={'layer':weights}))
    source=RuntimeSource(engine,Vault(tmp_path/'native'));source.characterize()
    assert source.descriptors[0]['s_m']==1.
    tile=next(source.tiles(source.descriptors[0]))
    np.testing.assert_array_equal(tile['base'],np.ones((2,64)))
    module.scales=mx.full((2,1),-.25,dtype=mx.bfloat16)
    source=RuntimeSource(engine,Vault(tmp_path/'signed'));source.characterize()
    tile=next(source.tiles(source.descriptors[0]))
    expected=np.asarray(mx.dequantize(weights,module.scales,module.biases,group_size=64,bits=4).astype(mx.float32))
    np.testing.assert_array_equal(tile['base'],expected)
    assert 'signed-scale' in source.manifest(configuration())['restoration_rule']
    delta=np.full((2,64),.375)
    codes,_=quantize(tile,delta)
    np.testing.assert_array_equal(codes,np.full((2,64),6,np.uint8))


def test_blind_interactive_sessions_are_independent_and_readonly(tmp_path):
    from activation_field.transforms.jobs import TransformJobs
    jobs=TransformJobs(tmp_path,'manifest',launch=False)
    with jobs.db:jobs.db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,NULL,NULL,0)',('compare',1,'succeeded','compare','encrypted'))
    Vault(jobs.jobs/'compare/inputs').save_json('config.enc',dict(target_checkpoint='target',restored_checkpoint='restored'))
    vault=Vault(tmp_path/'state')
    for name in ('target','restored'):vault.save(name,dict(baseline_hash='a'*64,source_transform='b'*64,source_transform_bytes=1234),[])
    class Sessions:
        def __init__(self):self.vault=vault;self.states={};self.requests={}
        def create(self,body):
            if body['request_id'] in self.requests:return self.requests[body['request_id']]
            sid=str(len(self.states));state=dict(session_id=sid,version=0,config=body['config'],checkpoint=None)
            self.states[sid]=state;self.requests[body['request_id']]=state.copy();return state.copy()
        def get(self,sid):return self.states[sid]
        def save(self,state):self.states[state['session_id']]=state
    sessions=Sessions();body=dict(request_id='trial1',expected_version=1,blind=True)
    receipt=jobs.trial('compare',body,sessions)
    assert receipt==jobs.trial('compare',body,sessions)
    assert set(receipt['sessions'])=={'A','B','C'} and len(sessions.states)==3
    assert sum(bool(s['checkpoint']) for s in sessions.states.values())==2
    assert all(s['config']['read_only'] and not s['config']['enabled'] for s in sessions.states.values())
    assert receipt['artifact_bytes']==1234
