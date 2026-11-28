import unittest
import numpy as np
from experiments.stability import suite,corpus as stability_corpus,normalize
from experiments.evaluate import validate_config


class StudyControls(unittest.TestCase):
    def test_stability_oracles_and_identity(self):
        items=suite()
        self.assertEqual(len(items),12)
        self.assertEqual({x['kind'] for x in items},{'arithmetic','code','constraint'})
        self.assertEqual(stability_corpus(),stability_corpus())
        self.assertEqual(normalize('  YES \n please '),'yes please')
        self.assertEqual(items[0]['answer'],'29')
        self.assertEqual(items[1]['answer'],'74')
        self.assertEqual(items[2]['answer'],'14')

    def test_config_new_studies(self):
        for kind in ('memory','stability','descriptors'):validate_config({'kind':kind})
        for config in ({'kind':'unknown'},{'tokens':0},{'checkpoint_tokens':-1},{'examples_per_template':False}):
            with self.assertRaises(ValueError):validate_config(config)

    def test_reasoning_corpus_labels_and_splits(self):
        from experiments.descriptors import corpus
        data=corpus(42,2)
        self.assertEqual(data,corpus(42,2))
        self.assertEqual(len(data['rows']),108)
        for row in data['rows']:self.assertEqual(row['valid'],row['expected']==row['claimed'])
        splits=[{r['template_id'] for r in data['rows'] if r['split']==s} for s in ('train','validation','test')]
        for i in range(3):
            for j in range(i):self.assertFalse(splits[i]&splits[j])
        pairs={}
        for row in data['rows']:pairs.setdefault(row['conversation'],[]).append(row)
        self.assertTrue(all({r['valid'] for r in pair}=={True,False} for pair in pairs.values()))

    def test_metrics_ties_and_perfect_ranking(self):
        from experiments.descriptors import metrics
        y=np.array([0,1,0,1])
        self.assertEqual(metrics(y,np.full(4,.5))['auroc'],.5)
        r=metrics(y,np.array([.1,.9,.2,.8]))
        self.assertEqual(r['auroc'],1.)
        self.assertEqual(r['precision'],1.)
        self.assertEqual(r['recall'],1.)

    def test_classifier_never_fits_heldout_labels(self):
        from experiments.descriptors import evaluate
        rows=[]
        for split in ('train','validation','test'):
            for domain in ('arithmetic','code'):
                for valid in (False,True):
                    for i in range(2):
                        value=float(valid)+i*.01
                        d=dict(eigenvalues=[value],heat_trace={'1':value},components=1,isolated_nodes=0,singular_values=[value],antisymmetric_norm=value,layer_flow=[[value]],layer_mean=[value],layer_variance=[0.],self_persistence=[value],autocorrelation={str(k):[value] for k in (1,2,4,8)})
                        identity=f'{split}-{domain}-{valid}-{i}'
                        rows.append(dict(id=identity,conversation=identity,template_id=split+domain,split=split,domain=domain,pattern='sum',difficulty=1,field_age=0,valid=valid,token_count=128,activation_mean=[value],descriptors=[d]))
        first=evaluate(rows)
        self.assertEqual(first['methods']['graph']['coherence']['auroc'],1.)
        for row in rows:
            if row['split']=='test':row['valid']=not row['valid']
        second=evaluate(rows)
        self.assertEqual(second['methods']['graph']['coherence']['auroc'],0.)
        self.assertEqual(first['methods']['graph']['classifier_weights'],second['methods']['graph']['classifier_weights'])
        rows[-1]['template_id']=rows[0]['template_id']
        with self.assertRaisesRegex(ValueError,'leakage'):evaluate(rows)

    def test_topology_known_cycle(self):
        try:import ripser
        except ImportError:self.skipTest('optional ripser unavailable')
        from activation_field.spectra import persistence
        # Four cycle edges appear at .5; diagonals at 1 close the H1 hole.
        similarity=np.array([[0,.5,0,.5],[.5,0,.5,0],[0,.5,0,.5],[.5,0,.5,0.]])
        # max(S)=.5 gives edges0, diagonals1, hence one interval [0,1].
        result=persistence(similarity)
        self.assertEqual(result['H0']['essential_count'],1)
        self.assertAlmostEqual(result['H1']['max_persistence'],1.)

if __name__=='__main__':unittest.main()
