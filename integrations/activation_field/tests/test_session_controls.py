"""Session controls without a model/GPU; native model tests remain separate."""
import tempfile,unittest,threading
from unittest.mock import patch
from activation_field.session import Sessions,Conflict
from activation_field.config import FieldConfig

class SessionControls(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        p=patch('activation_field.session.read_manifest',return_value=dict(baseline_hash='test'))
        p.start();self.addCleanup(p.stop);self.sessions=Sessions(self.tmp.name,'unused')

    def test_versioned_configuration_and_retries(self):
        sessions=self.sessions
        sid=sessions.create(dict(request_id='new',persist=True))['session_id']
        body=dict(request_id='config',expected_version=0,config=dict(enabled=True,baseline_pull=.0001))
        a=sessions.configure(sid,body)
        self.assertEqual((a['status'],a['version']),('succeeded',1))
        self.assertTrue(sessions.get(sid)['config']['enabled'])
        self.assertEqual(sessions.configure(sid,body),a)
        with self.assertRaises(Conflict):sessions.configure(sid,dict(body,config=dict(enabled=False)))
        with self.assertRaises(Conflict):sessions.configure(sid,dict(body,request_id='stale'))
        self.assertEqual(sessions.get(sid)['version'],1)

    def test_isolated_sessions_and_restart(self):
        sessions=self.sessions
        a=sessions.create(dict(request_id='a',persist=True));b=sessions.create(dict(request_id='b'))
        sessions.configure(a['session_id'],dict(request_id='on',expected_version=0,config=dict(enabled=True)))
        self.assertFalse(sessions.get(b['session_id'])['config']['enabled'])
        restored=Sessions(sessions.root,'unused')
        self.assertTrue(restored.get(a['session_id'])['config']['enabled'])
        self.assertTrue(restored.get(b['session_id'])['closed'])

    def test_live_configuration_waits_for_boundary(self):
        sessions=self.sessions;sid=sessions.create(dict(request_id='new'))['session_id']
        s=sessions.get(sid);s['busy']='generation';sessions.save(s)
        body=dict(request_id='change',expected_version=0,config=dict(eta=.001))
        self.assertEqual(sessions.configure(sid,body)['status'],'queued')
        self.assertEqual(sessions.get(sid)['config']['eta'],.0001)
        class Engine:
            config=FieldConfig();consumed_tokens=23
            field=type('Field',(),dict(version=18))()
            def patch_config(self,patch):self.config=FieldConfig.parse(dict(self.config.to_dict(),**patch))
        sessions.apply_config(sid,Engine())
        receipt=sessions.receipt(sid,'change')
        self.assertEqual(receipt['status'],'succeeded')
        self.assertEqual(receipt['result']['effective_boundary'],23)
        self.assertEqual(sessions.get(sid)['config']['eta'],.001)

    def test_restart_marks_inflight_interrupted_without_replay(self):
        sessions=self.sessions;sid=sessions.create(dict(request_id='new',persist=True))['session_id']
        receipt,fresh=sessions.begin(sid,'generate',dict(request_id='running',expected_version=0))
        self.assertTrue(fresh)
        restarted=Sessions(sessions.root,'unused')
        self.assertEqual(restarted.receipt(sid,'running')['status'],'interrupted')
        self.assertIsNone(restarted.get(sid)['busy'])
        _,fresh=restarted.begin(sid,'generate',dict(request_id='running',expected_version=0))
        self.assertFalse(fresh)

    def test_start_reserves_session_before_worker_runs(self):
        sessions=self.sessions;sid=sessions.create(dict(request_id='new'))['session_id']
        body=dict(request_id='one',expected_version=0,messages=[])
        with patch.object(threading.Thread,'start'):
            sessions.start(sid,body)
            self.assertEqual(sessions.get(sid)['busy'],'one')
            sessions.start(sid,body)
            with self.assertRaises(Conflict):sessions.start(sid,dict(body,request_id='two'))
            with self.assertRaises(Conflict):sessions.start(sid,dict(body,messages=[dict(role='user',content='changed')]))

if __name__=='__main__':unittest.main()
