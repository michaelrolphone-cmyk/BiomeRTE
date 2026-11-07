import unittest
import numpy as np
from activation_field.boundaries import accumulate,metadata,tensors,restore,ReverseTrajectory


class BoundaryTests(unittest.TestCase):
    def test_fixed_aggregate_storage_and_context_replacement(self):
        record=None
        for i in range(30):
            x=np.array([1.,i%3],np.float32)
            record=accumulate(record,x,x,{'memory':np.array([float(i)],np.float32)},1)
        self.assertEqual(record.samples,30)
        self.assertEqual(record.arrays['backward_flow'].shape,(2,1))
        self.assertEqual(record.arrays['context.memory'][0],29.)
        self.assertGreater(record.motion,0)

    def test_checkpoint_format_and_corruption(self):
        r=accumulate(None,[1.,2.],[3.,4.],{},1)
        meta=metadata({'op':r},7)
        values=dict(tensors({'op':r}))
        loaded=restore(meta,values,7,{'op':(2,2,{})})
        np.testing.assert_array_equal(loaded['op'].arrays['input_mean'],r.arrays['input_mean'])
        with self.assertRaises(ValueError):restore(meta,values,8,{'op':(2,2,{})})
        with self.assertRaises(ValueError):restore(meta,{},7,{'op':(2,2,{})})
        self.assertEqual(restore(None,{},7,{'op':(2,2,{})}),{})

    def test_trajectory_uses_measured_motion_not_fixed_length(self):
        r=None
        for i in range(13):
            y=[np.cos(i*.1),np.sin(i*.1)]
            r=accumulate(r,y,y,{},1)
        trajectory=ReverseTrajectory(r)
        steps=0
        while not trajectory.done:
            steps+=1
            trajectory.advance()
            self.assertLess(steps,100)  # Test harness runaway guard, not production limit.
        self.assertGreater(steps,8)
        self.assertEqual(trajectory.remaining,0)
        single=ReverseTrajectory(accumulate(None,[1.],[1.],{},1))
        single.advance()
        self.assertTrue(single.done)
