import unittest
import numpy as np
from activation_field.inverse import InverseOptions,LegacyReplayOptions,delta_basis,beam_reconstruct,first_pass_result


class InverseMechanics(unittest.TestCase):
    def test_reverse_is_the_only_service_mode(self):
        self.assertEqual(InverseOptions.parse().mode,'reverse')
        for mode in ('first_pass', 'replay', 'guess'):
            with self.assertRaises(ValueError):InverseOptions.parse({'mode':mode})

    def test_first_pass_returns_all_ranked_tokens_without_quality_gate(self):
        proposals=[dict(token_id=5,proposal_score=0.),dict(token_id=2,proposal_score=-1.)]
        decode=lambda ids: ''.join({5:' odd',2:' output'}[i] for i in ids)
        result=first_pass_result(proposals,decode)
        self.assertEqual(result['context_text'],' odd output')
        self.assertEqual(result['estimated_token_ids'],[5,2])
        self.assertEqual(result['replay_evaluations'],0)
        self.assertIsNone(result['best_relative_squared_error'])
        self.assertFalse(result['sequence_order_inferred'])
        self.assertEqual(result['candidate_kind'],'ranked_token_hypotheses')
        self.assertEqual([c['token_ids'] for c in result['candidates']],[[5],[2]])

    def test_recovers_input_direction_from_signed_outer_product(self):
        rng=np.random.default_rng(7);x=rng.normal(size=128);x/=np.linalg.norm(x)
        coefficients=rng.normal(size=40);delta=np.outer(coefficients,x)
        rows=np.repeat(np.arange(40),2);groups=np.tile(np.arange(2),40)
        basis,info=delta_basis([(rows,groups,delta.reshape(-1,64),40)],128,1,2,17)
        self.assertGreater(abs(float(basis[:,0]@x)),.99999)
        self.assertEqual(info['rank'],1)

    def test_zero_delta_produces_no_invented_direction(self):
        basis,info=delta_basis([(np.array([0]),np.array([0]),np.zeros((1,64)),1)],64,4,2,1)
        self.assertEqual(basis.shape,(64,0));self.assertEqual(info['energy'],0)

    def test_invalid_coordinates_and_nonfinite_delta_rejected(self):
        for rows,groups,values in [(np.array([2]),np.array([0]),np.ones((1,64))),
                                    (np.array([0]),np.array([1]),np.ones((1,64))),
                                    (np.array([0]),np.array([0]),np.full((1,64),np.nan))]:
            with self.assertRaises(ValueError):delta_basis([(rows,groups,values,1)],64,1,1,1)

    def test_replay_selects_order_from_aggregate_candidates(self):
        def score(seq):
            desired=(3,1)
            return {'relative_squared_error':sum(a!=b for a,b in zip(seq,desired))+abs(len(seq)-2)}
        result=beam_reconstruct([1,3],score,LegacyReplayOptions(max_tokens=2,beam_width=2))
        self.assertEqual(result[0]['token_ids'],[3,1]);self.assertEqual(result[0]['relative_squared_error'],0)

    def test_search_settings_are_explicit_and_not_hidden_hard_caps(self):
        with self.assertRaises(ValueError):InverseOptions.parse({'max_tokens':10000})
        for bad in ({'max_tokens':0},{'beam_width':True},{'seed':-1},{'unknown':1},{'layout':'guess'}):
            with self.assertRaises(ValueError):InverseOptions.parse(bad)

    def test_invalid_replay_measurement_does_not_publish_candidate(self):
        with self.assertRaises(ValueError):beam_reconstruct([1],lambda seq:{'relative_squared_error':float('nan')},LegacyReplayOptions())


if __name__=='__main__':unittest.main()

