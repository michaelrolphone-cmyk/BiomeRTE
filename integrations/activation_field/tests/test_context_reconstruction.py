import hashlib,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
from activation_field.checkpoint import Vault,canonical
from activation_field.context_reconstruction.snapshot import snapshot,weight_arrays
from activation_field.context_reconstruction.jobs import ContextJobs
from activation_field.context_reconstruction.capabilities import adapter,bind_contract
from activation_field.context_reconstruction.report import context_artifact
from activation_field.context_reconstruction.transport import transport_result,read_transfer

class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);v=Vault(self.root/'state')
        self.meta=dict(baseline_hash='abc',tensor_map_version=1,kind='complete',config={},
                       prefix_hashes=['private'],last_logits='private',cache='private')
        v.save('target',self.meta,[('p.indices',np.array([0],np.uint32)),('p.codes',np.ones((1,8),np.uint32)),('cache.0',np.array([99.]))])
        self.reader=patch('activation_field.context_reconstruction.jobs.read_manifest',return_value={'baseline_hash':'abc'})
        self.reader.start();self.addCleanup(self.reader.stop)
        self.jobs=ContextJobs(self.root,'manifest',launch=False);self.addCleanup(self.jobs.db.close)
        self.body=dict(request_id='first',expected_version=0,target_checkpoint='target',options={'mode':'reverse'})
    def test_source_snapshot_never_opens_cache_shards(self):
        v=Vault(self.root/'state');m=v.manifest('target')
        cache=next(i for i in m['tensors'] if i['name']=='cache.0')
        for c in cache['chunks']:(v.root/'target'/c['file']).write_bytes(b'unreadable secret cache')
        receipt=self.jobs.start('reconstruct',self.body)
        frozen=Vault(self.jobs.jobs/receipt['run_id']/'source')
        self.assertEqual(set(frozen.manifest('source')['metadata']),{'baseline_hash','tensor_map_version','kind','config'})
        self.assertEqual([k for k,_ in frozen.arrays('source')],['p.indices','p.codes'])
    def test_idempotency_and_changed_request(self):
        r=self.jobs.start('reconstruct',self.body)
        self.assertEqual(r,self.jobs.start('reconstruct',self.body))
        with self.assertRaises(ValueError):self.jobs.start('reconstruct',dict(self.body,purpose='different'))
    def test_snapshot_survives_source_deletion(self):
        r=self.jobs.start('reconstruct',self.body);Vault(self.root/'state').delete('target')
        self.assertEqual(len(list(Vault(self.jobs.jobs/r['run_id']/'source').arrays('source'))),2)
    def test_input_and_result_encrypted_and_export_explicit(self):
        r=self.jobs.start('reconstruct',self.body);rid=r['run_id']
        secret={'context_text':'synthetic private context','status':'complete'}
        Vault(self.jobs.jobs/rid/'result').save_json('result.enc',secret)
        self.jobs._finish(rid,'succeeded')
        self.assertNotIn('synthetic private context',json.dumps(self.jobs.get(rid)))
        with self.assertRaises(ValueError):self.jobs.export(dict(run_id=rid,expected_version=1))
        with self.assertRaises(ValueError):self.jobs.export(dict(run_id=rid,expected_version=0,allow_plaintext=True))
        self.assertEqual(self.jobs.export(dict(run_id=rid,expected_version=1,allow_plaintext=True))['artifact'],secret)
        self.assertNotIn(b'synthetic private context',(self.jobs.jobs/rid/'result/result.enc').read_bytes())
    def test_cancellation_version_and_resume(self):
        r=self.jobs.start('reconstruct',self.body);rid=r['run_id']
        body=dict(request_id='cancel',expected_version=0)
        self.assertEqual(self.jobs.cancel(rid,body),self.jobs.cancel(rid,body))
        self.jobs._finish(rid,'cancelled')
        with self.assertRaises(ValueError):self.jobs.start('reconstruct',dict(request_id='resume-old',resume_run_id=rid,expected_version=0))
        r2=self.jobs.start('reconstruct',dict(request_id='resume',resume_run_id=rid,expected_version=2))
        self.assertEqual(rid,r2['run_id'])
    def test_worker_seals_complete_result_including_cost_digest(self):
        r=self.jobs.start('reconstruct',self.body);rid=r['run_id']
        value=dict(status='complete',context_text='synthetic',candidates=[{'token_ids':[7]}],
                   target_squared_norm=1,uncertainty_and_gaps=['Unknown history'])
        with patch('activation_field.inverse_runtime.reconstruct',return_value=value):
            self.jobs.worker(rid)
        status=self.jobs.get(rid);self.assertEqual(status['status'],'succeeded')
        report=self.jobs.export(dict(run_id=rid,expected_version=status['version'],allow_plaintext=True))['artifact']
        integrity=report.pop('integrity')
        self.assertEqual(integrity['sha256'],hashlib.sha256(canonical(report)).hexdigest())
        self.assertIn('cost',report)
    def test_boolean_versions_rejected(self):
        r=self.jobs.start('reconstruct',self.body)
        with self.assertRaises(ValueError):self.jobs.cancel(r['run_id'],dict(request_id='bad',expected_version=False))
    def test_dead_worker_recovery(self):
        r=self.jobs.start('reconstruct',self.body)
        with self.jobs.db:self.jobs.db.execute("UPDATE jobs SET state='running' WHERE id=?",(r['run_id'],))
        self.jobs.reconcile()
        self.assertEqual(self.jobs.get(r['run_id'])['status'],'interrupted')
    def test_contract_route_bound_to_offer(self):
        p=dict(operation='id',inputs={'arguments':{'run_id':'abc'}})
        out=adapter(bind_contract(p,'status'),lambda path:dict(path=path))
        self.assertEqual(out['path'],'/context/jobs/abc')
        with self.assertRaises(ValueError):bind_contract(dict(p,contract='wrong'),'status')
    def test_current_usage_preserves_immutable_v1_descriptions(self):
        from activation_field.context_reconstruction.capabilities import offers
        class Registry:
            def invoke(self,operation,body,rid=None):
                self_test.assertEqual(operation,'cap.define')
                action=body['name'].split('.')[-1]
                self_test.assertEqual(body['description'],'Weight changes to context: '+action)
        self_test=self
        result=offers(Registry(),'test-resource')
        self.assertEqual(len(result),4)
        self.assertIn('mode=reverse only',result[0]['usage']['parameters']['arguments'])
    def test_export_digest_and_hypothesis_labels(self):
        result=dict(context_text='jade',candidates=[{'token_ids':[42]}],target_squared_norm=1,uncertainty_and_gaps=['Unknown history'])
        a=context_artifact(result,{'sha256':'abc'},'handoff','export')
        integrity=a.pop('integrity');self.assertEqual(integrity['sha256'],hashlib.sha256(canonical(a)).hexdigest())
        self.assertEqual(a['evidence_map'][1]['kind'],'hypothesis')
        self.assertIn('Not performed',a['reinjection'])
    def test_ranked_tokens_export_all_ids_without_sequence_claim(self):
        from activation_field.inverse import first_pass_result
        result=first_pass_result([dict(token_id=4,proposal_score=.1),
                                  dict(token_id=2,proposal_score=.05)],lambda ids: str(ids))
        result['target_squared_norm']=1.
        a=context_artifact(result,{'sha256':'abc'},'first pass','export')
        self.assertEqual(a['estimated_token_ids'],[4,2])
        self.assertEqual(a['estimated_context'],'[4, 2]')
        self.assertIn('not recovered source order',a['context_text'])
        self.assertIn('no replay',a['evidence_map'][1]['measurement'])
        integrity=a.pop('integrity')
        self.assertEqual(integrity['sha256'],hashlib.sha256(canonical(a)).hexdigest())

    def test_small_signed_context_preserves_float_spelling(self):
        blobs={}
        def put(data):
            h=hashlib.sha256(data).hexdigest();blobs[h]=data;return dict(sha256=h,size=len(data))
        report=context_artifact(dict(context_text='jade',candidates=[{'token_ids':[42],'relative_squared_error':0.0}],
                target_squared_norm=1.0,uncertainty_and_gaps=['Unknown history'],negative_zero=-0.0),{'sha256':'abc'},'test','id')
        wrapped=transport_result(dict(ok=True,artifact=report),put)
        self.assertIn('result_artifact',wrapped)
        # Simulate the controller's numeric normalization of the outer envelope.
        wire=json.loads(json.dumps(wrapped),parse_float=lambda s:int(float(s)) if float(s).is_integer() else float(s))
        raw=read_transfer(wire['result_artifact'],blobs.__getitem__)
        restored=json.loads(raw)['artifact'];integrity=restored.pop('integrity')
        self.assertEqual(integrity['sha256'],hashlib.sha256(canonical(restored)).hexdigest())
        self.assertIn(b'"negative_zero":-0.0',raw)
        self.assertIn(b'"relative_squared_error":0.0',raw)

    def test_large_report_transport_is_complete(self):
        blobs={}
        def put(data):
            h=hashlib.sha256(data).hexdigest();blobs[h]=data;return dict(sha256=h,size=len(data))
        value=dict(ok=True,result='x'*150000)
        wrapped=transport_result(value,put)
        self.assertEqual(json.loads(read_transfer(wrapped['result_artifact'],blobs.__getitem__)),value)

if __name__=='__main__':unittest.main()

