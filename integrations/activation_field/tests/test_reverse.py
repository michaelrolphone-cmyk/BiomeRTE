"""Wiring/number-format tests only; no model output-quality claim."""
import unittest
from unittest.mock import Mock
import numpy as np
from activation_field.reverse import Operation, ReverseUnavailable, reverse_operations, quantized_backprojection
from activation_field.inverse_runtime import reconstruct_with_adapter
from activation_field.quantized_writer import pack
from activation_field.context_reconstruction.report import context_artifact


class ReverseMechanics(unittest.TestCase):
    def test_reverse_graph_uses_every_counterpart_in_reverse_order(self):
        operations = (Operation('first',('input',),'middle'), Operation('second',('middle',),'output'))
        calls = []
        weights = object()
        def counterpart(name, edge):
            def apply(signal, direction, current_weights):
                calls.append(name)
                self.assertIs(current_weights, weights)
                self.assertEqual(direction, name)
                return {edge:signal}
            return apply
        result = reverse_operations(operations, {'output':[3.,4.]},
            {'first':'first','second':'second'}, weights,
            {'first':counterpart('first','input'),'second':counterpart('second','middle')})
        self.assertEqual(calls,['second','first'])
        np.testing.assert_allclose(result['input'],[.6,.8])

    def test_branches_merge_and_missing_counterparts_do_not_silently_skip(self):
        ops = (Operation('left',('x',),'l'), Operation('right',('x',),'r'), Operation('merge',('l','r'),'out'))
        counterparts = {'left':lambda *_:{'x':np.array([1.,0.])},
                        'right':lambda *_:{'x':np.array([0.,1.])},
                        'merge':lambda *_:{'l':np.array([1.,0.]),'r':np.array([0.,1.])}}
        flow = reverse_operations(ops,{'out':[1.,1.]},dict.fromkeys(counterparts,{}),{},counterparts)
        np.testing.assert_allclose(flow['x'],np.ones(2)/np.sqrt(2))
        with self.assertRaises(ReverseUnavailable):
            reverse_operations(ops,{'out':[1.,1.]},{},{},{})

    def test_tiled_current_weight_projection_counterpart(self):
        codes = np.full((3,128), 7, np.uint32)
        codes[0,:64] = 9
        codes[1,64:] = 4
        scales = np.array([[.1,-.2]]*3,np.float32)
        offsets = np.array([[.05,.03]]*3,np.float32)
        output = np.array([1.,-2.,.5],np.float32)
        weight = codes.astype(np.float32)*np.repeat(scales,64,axis=1)+np.repeat(offsets,64,axis=1)
        expected = output@weight
        expected /= np.linalg.norm(expected)
        for rows in (1,2,3):
            np.testing.assert_allclose(quantized_backprojection(output,pack(codes),scales,offsets,rows),expected,rtol=1e-6)

    def test_adapter_emits_until_trajectory_end_not_an_eight_token_limit(self):
        # Deliberately trivial adapter exercises the interface, not Qwen inference.
        class WiringAdapter:
            operations = (Operation('op',('embedding',),'current'),)
            counterparts = {'op':lambda signal,*_:{'embedding':signal}}
            def begin(self,*_):return 0
            def finished(self,state):return state == 19
            def signals(self,state):return {'current':np.array([1.,0.])}
            def readout(self,flow,state):return state
            def advance(self,state,flow):return state+1
        result = reconstruct_with_adapter(WiringAdapter(),{}, {'op':{}},{},lambda ids:','.join(map(str,ids)))
        self.assertEqual(result['estimated_token_ids'],list(range(19)))
        artifact = context_artifact(result,{'sha256':'source'},'test','id')
        self.assertEqual(artifact['context_text'],result['context_text'])
        self.assertNotIn('candidates',artifact)
        self.assertNotIn('evidence_map',artifact)
        self.assertNotIn('reinjection',artifact)

    def test_readout_end_and_cancellation_are_respected(self):
        adapter = Mock()
        adapter.begin.return_value = object()
        adapter.finished.return_value = False
        adapter.operations = ()
        adapter.signals.return_value = {'embedding':np.array([1.])}
        adapter.counterparts = {}
        adapter.readout.return_value = None
        result = reconstruct_with_adapter(adapter,{}, {},{},str)
        self.assertEqual(result['estimated_token_ids'],[])
        adapter.advance.assert_not_called()
        def cancel():raise InterruptedError('cancelled')
        with self.assertRaises(InterruptedError):
            reconstruct_with_adapter(adapter,{}, {},{},str,check=cancel)
