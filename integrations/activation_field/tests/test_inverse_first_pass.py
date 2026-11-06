"""First-pass integration regression: model replay must never be called."""
import hashlib
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch
import numpy as np
from activation_field.checkpoint import canonical
from activation_field.inverse_runtime import reconstruct


class FirstPassRuntime(unittest.TestCase):
    def test_default_returns_vocabulary_pass_without_sequence_search(self):
        manifest={'baseline_hash':'baseline'}
        source={'metadata':{'config':{}}}
        vault=Mock()
        vault.manifest.return_value=source
        target=Mock()
        target.source_hash=hashlib.sha256(canonical(source)).hexdigest()
        target.changed_groups=2
        target.energy=1.
        target.proposal_blocks.return_value=([],64,['projection'])
        engine=Mock()
        engine.tokenizer.decode.side_effect=lambda ids: ''.join({7:' raw',3:' output'}[i] for i in ids)
        proposals=[dict(token_id=7,proposal_score=.01),dict(token_id=3,proposal_score=0.)]
        with patch('activation_field.runtime.Engine',return_value=engine), \
             patch('activation_field.admission.model_slot',return_value=nullcontext()), \
             patch('experiments.storage.source_identity',return_value='source'), \
             patch('activation_field.inverse_runtime.DeltaTarget',return_value=target), \
             patch('activation_field.inverse_runtime.delta_basis',return_value=(np.ones((64,1)),{'rank':1})), \
             patch('activation_field.inverse_runtime.propose_tokens',return_value=proposals) as propose, \
             patch('activation_field.inverse_runtime.beam_reconstruct',side_effect=AssertionError('Replay called')) as replay:
            result=reconstruct(manifest,vault,'checkpoint')
        propose.assert_called_once()
        replay.assert_not_called()
        target.score.assert_not_called()
        engine.patch_config.assert_not_called()
        engine.consume.assert_not_called()
        engine.reset.assert_not_called()
        engine.close.assert_called_once()
        self.assertEqual(result['context_text'],' raw output')
        self.assertEqual(result['estimated_token_ids'],[7,3])
        self.assertEqual(result['replay_evaluations'],0)
        self.assertEqual(result['method'],'delta_gate_signature_vocabulary_first_pass')
        self.assertFalse(result['sequence_order_inferred'])
        self.assertFalse(result['source_modified'])


if __name__=='__main__':unittest.main()
