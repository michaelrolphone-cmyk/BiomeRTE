"""CPU regression tests for experiment durability and import validation."""
import hashlib
import json
import tempfile
import threading
import unittest
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import patch
import numpy as np
from activation_field.change_validation import validate_entry
from activation_field.experiment_jobs import ExperimentJobs
from experiments.corpus import corpus
from experiments.evaluate import validate_config
from experiments.report import paired, report
from experiments.storage import atomic_json, canonical, digest, prepare, run_lock


class CompletionControls(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_corpus_deterministic_disjoint_and_corrections(self):
        a = corpus(11, 8)
        self.assertEqual(a, corpus(11, 8))
        self.assertNotEqual(a['sha256'], corpus(23, 8)['sha256'])
        templates = {s: {e['template_id'] for e in a['episodes'] if e['split'] == s} for s in ('development','heldout')}
        self.assertFalse(templates['development'] & templates['heldout'])
        for e in a['episodes']:
            self.assertEqual(sum(p['corrected'] and p['kind']=='forward' for p in e['probes']),2)
            self.assertEqual({p['kind'] for p in e['probes']},{'forward','reverse','preference'})
        self.assertEqual(digest({k:v for k,v in a.items() if k!='sha256'}),a['sha256'])

    def test_resume_identity_and_frozen_corpus(self):
        data=corpus(42,2);config={'episodes':2}
        first=prepare(self.root,config,data,'baseline',False)
        self.assertEqual(first,prepare(self.root,config,data,'baseline',True))
        for cfg,base in [({'episodes':4},'baseline'),(config,'different')]:
            with self.assertRaises(ValueError):prepare(self.root,cfg,data,base,True)
        with self.assertRaises(ValueError):prepare(self.root,config,data,'baseline',False)
        saved=json.loads((self.root/'corpus.json').read_text());saved['episodes'][0]['training']='tampered'
        atomic_json(self.root/'corpus.json',saved)
        with self.assertRaises(ValueError):prepare(self.root,config,data,'baseline',True)

    def test_legacy_results_never_overwritten(self):
        atomic_json(self.root/'results.json',[{'original':True}])
        with self.assertRaises(ValueError):prepare(self.root,{},corpus(42,2),'base',True)
        self.assertEqual(json.loads((self.root/'results.json').read_text()),[{'original':True}])

    def test_atomic_write_retains_old_on_invalid_value(self):
        path=self.root/'value.json';atomic_json(path,{'old':True})
        with self.assertRaises(ValueError):atomic_json(path,{'bad':float('nan')})
        self.assertEqual(json.loads(path.read_text()),{'old':True})

    def test_exclusive_run_lock(self):
        with run_lock(self.root):
            with self.assertRaises(ValueError):
                with run_lock(self.root):pass
        with run_lock(self.root):pass

    def test_config_rejects_silent_mistakes(self):
        validate_config({'seeds':[11,23],'retention_gaps':[0,512]})
        for bad in [{'conditions':['C2','C2']},{'conditions':['oops']},{'seeds':[1,1]}, {'seeds':[True]}, {'episodes':1}, {'spectral':1},{'typo':2}]:
            with self.assertRaises(ValueError):validate_config(bad)

    @staticmethod
    def rows():
        return [dict(condition=c,seed=s,episode=e,gap=0,split='heldout',status='complete',answers=[dict(exact=c=='C2',corrected=True)])
                for c in ('C1','C2') for s in (11,23,37) for e in range(4)]

    def test_bootstrap_clusters_seeds_by_episode(self):
        result=paired(self.rows(),'C2','C1',0)
        self.assertEqual(result['paired_trials'],12)
        self.assertEqual(result['paired_episodes'],4)
        self.assertEqual(result['ci95'],[1.,1.])
        self.assertEqual(result['difference'],1.)

    def test_missing_controls_and_duplicate_trials(self):
        result=paired(self.rows(),'C2','C3',0)
        self.assertEqual(result['status'],'insufficient_evidence')
        self.assertIsNone(result['ci95'])
        rows=self.rows()
        with self.assertRaises(ValueError):paired(rows+[rows[0]],'C2','C1',0)

    def test_report_does_not_invent_scientific_success(self):
        atomic_json(self.root/'results.json',self.rows())
        atomic_json(self.root/'config.json',{'retention_gaps':[0]})
        result=report(self.root,self.root/'report')
        self.assertEqual(result['scientific_success'],'not_established')
        self.assertTrue(any(c['status']=='insufficient_evidence' for c in result['comparisons']))

    def test_job_retry_conflict_and_restart_resume(self):
        jobs=ExperimentJobs(self.root,'manifest')
        self.addCleanup(jobs.db.close)
        body=dict(request_id='new',expected_version=0,config={'episodes':2})
        with patch.object(threading.Thread,'start'):
            receipt=jobs.start(body)
            self.assertEqual(receipt,jobs.start(body))
            with self.assertRaises(ValueError):jobs.start(dict(body,config={'episodes':4}))
            restarted=ExperimentJobs(self.root,'manifest');self.addCleanup(restarted.db.close)
            state=restarted.get(receipt['run_id'])
            self.assertEqual(state['status'],'interrupted')
            self.assertEqual(state['version'],1)
            with self.assertRaises(ValueError):restarted.start(dict(request_id='bad',run_id=receipt['run_id'],expected_version=0))
            resumed=restarted.start(dict(request_id='resume',run_id=receipt['run_id'],expected_version=1))
            self.assertEqual(resumed['run_id'],receipt['run_id'])
            self.assertEqual(resumed['version'],2)

    def test_job_failure_has_durable_error(self):
        def fail(config,resume):raise RuntimeError('synthetic failure')
        jobs=ExperimentJobs(self.root,'manifest',runner=fail);self.addCleanup(jobs.db.close)
        with patch.object(threading.Thread,'start'):
            receipt=jobs.start(dict(request_id='one',expected_version=0,config={}))
        jobs._run(receipt['run_id'],{},False)
        state=jobs.get(receipt['run_id'])
        self.assertEqual(state['status'],'failed')
        self.assertIn('synthetic failure',state['error'])

    def test_job_cannot_choose_arbitrary_output(self):
        jobs=ExperimentJobs(self.root,'manifest');self.addCleanup(jobs.db.close)
        for key in ('manifest','output'):
            with self.assertRaises(ValueError):jobs.start(dict(request_id=key,expected_version=0,config={key:'/tmp/unrelated'}))

    def test_job_exposes_measured_report_without_raw_inputs(self):
        jobs=ExperimentJobs(self.root,'manifest');self.addCleanup(jobs.db.close)
        with patch.object(threading.Thread,'start'):
            receipt=jobs.start(dict(request_id='stability',expected_version=0,config={'kind':'stability','tokens':8}))
        atomic_json(jobs.root/receipt['run_id']/'stability.json',dict(status='complete',consumed_tokens=8,meets_duration=False,stability_target_met=False,baseline={'answers':['synthetic source text']}))
        report=jobs.get(receipt['run_id'])['report']
        self.assertFalse(report['meets_duration']);self.assertEqual(report['consumed_tokens'],8)
        self.assertNotIn('baseline',report)

    def test_materialization_rejects_bad_map_without_resetting_engine(self):
        # A CPU fixture verifies the mutation boundary; Metal math remains separate.
        codes=np.zeros((1,8),np.uint32)
        entry=dict(part=0,module_path='projection',tensor_name='weight',logical_shape=[1,64],checksum='bad')
        class Vault:
            def manifest(self,name):return {'metadata':dict(kind='change_map',schema_version=1,tensor_map_version=1,baseline_hash='base',entries=[entry])}
            def arrays(self,name):
                yield '0.flat_indices',np.array([0],np.uint32)
                yield '0.replacement_packed',codes
                yield '0.signed_delta',np.zeros((1,64),np.float32)
        class Engine:
            manifest={'baseline_hash':'base'}
            field=types.SimpleNamespace(pending=[],entries=[('projection',types.SimpleNamespace(weight=codes))])
            def reset(self,*args):raise AssertionError('Active engine was reset')
            def state(self,*args,**kwargs):raise AssertionError('State touched before validation')
        fake=types.ModuleType('mlx');fake.core=types.ModuleType('mlx.core')
        with patch.dict(sys.modules,{'mlx':fake,'mlx.core':fake.core}):
            spec=importlib.util.spec_from_file_location('activation_field._changes_test',Path(__file__).parents[1]/'activation_field/changes.py')
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            with self.assertRaisesRegex(ValueError,'checksum'):
                module.FieldStore(Engine(),Vault()).materialize_state('base','map')
        self.assertTrue(np.array_equal(codes,np.zeros((1,8),np.uint32)))

    def test_map_layout_checksum_range_and_overlap(self):
        codes=np.arange(8,dtype=np.uint32).reshape(1,8)
        data=dict(flat_indices=np.array([0],np.uint32),replacement_packed=codes,signed_delta=np.zeros((1,64),np.float32))
        entry=dict(tensor_name='weight',logical_shape=[2,64],checksum=hashlib.sha256(codes.tobytes()).hexdigest())
        seen=set();validate_entry(entry,data,(2,8),seen)
        with self.assertRaises(ValueError):validate_entry(entry,data,(2,8),seen)
        for change in [dict(flat_indices=np.array([2],np.uint32)),dict(flat_indices=np.array([-1],np.int64)),dict(signed_delta=np.full((1,64),np.nan)),dict(replacement_packed=codes+1)]:
            with self.assertRaises(ValueError):validate_entry(entry,dict(data,**change),(2,8),set())
        with self.assertRaises(ValueError):validate_entry(dict(entry,logical_shape=[3,64]),data,(2,8),set())


if __name__=='__main__':unittest.main()
