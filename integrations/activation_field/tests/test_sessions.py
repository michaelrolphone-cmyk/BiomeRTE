import pytest
from activation_field.session import Sessions,Conflict

@pytest.fixture
def sessions(tmp_path,monkeypatch):
    monkeypatch.setattr('activation_field.session.read_manifest',lambda _:dict(baseline_hash='test'))
    return Sessions(tmp_path,'unused')

def test_versioned_configuration_and_retries(sessions):
    created=sessions.create(dict(request_id='new',persist=True));sid=created['session_id']
    body=dict(request_id='config',expected_version=0,config=dict(enabled=True,baseline_pull=.0001))
    a=sessions.configure(sid,body)
    assert a['status']=='succeeded' and a['version']==1
    assert sessions.get(sid)['config']['enabled'] is True
    assert sessions.configure(sid,body)==a
    with pytest.raises(Conflict):sessions.configure(sid,dict(body,config=dict(enabled=False)))
    with pytest.raises(Conflict):sessions.configure(sid,dict(body,request_id='stale'))
    assert sessions.get(sid)['version']==1

def test_isolated_sessions_and_restart(sessions):
    a=sessions.create(dict(request_id='a',persist=True));b=sessions.create(dict(request_id='b'))
    sessions.configure(a['session_id'],dict(request_id='on',expected_version=0,config=dict(enabled=True)))
    assert not sessions.get(b['session_id'])['config']['enabled']
    restored=Sessions(sessions.root,'unused')
    assert restored.get(a['session_id'])['config']['enabled']
    assert restored.get(b['session_id'])['closed']

def test_live_configuration_waits_for_boundary(sessions):
    from activation_field.config import FieldConfig
    sid=sessions.create(dict(request_id='new'))['session_id']
    s=sessions.get(sid);s['busy']='generation';sessions.save(s)
    body=dict(request_id='change',expected_version=0,config=dict(eta=.001))
    assert sessions.configure(sid,body)['status']=='queued'
    assert sessions.get(sid)['config']['eta']==.0001
    class Engine:
        config=FieldConfig();consumed_tokens=23
        field=type('Field',(),dict(version=18))()
        def patch_config(self,patch):self.config=FieldConfig.parse(dict(self.config.to_dict(),**patch))
    sessions.apply_config(sid,Engine())
    receipt=sessions.receipt(sid,'change')
    assert receipt['status']=='succeeded' and receipt['result']['effective_boundary']==23
    assert sessions.get(sid)['config']['eta']==.001
