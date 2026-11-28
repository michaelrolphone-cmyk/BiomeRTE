import unittest
import tempfile
from pathlib import Path
import numpy as np
from activation_field import directions
from activation_field.quantized_writer import write_coefficients
from activation_field.checkpoint import Vault
from activation_field.context_reconstruction.snapshot import snapshot, weight_arrays


class DirectionTests(unittest.TestCase):
    def record(self):
        return directions.accumulate(None, [1., 0.], [0., -1., 0.], [0., -.01, 0.], 1.)

    def test_aggregate_size_does_not_grow_with_tokens(self):
        record = self.record()
        size = sum(a.nbytes for a in record.arrays.values())
        for _ in range(100):
            record = directions.accumulate(record, [-1., 1.], [1., 0., 0.], [.01, 0., 0.], 1.)
        self.assertEqual(record.samples, 101)
        self.assertEqual(sum(a.nbytes for a in record.arrays.values()), size)
        self.assertEqual(set(record.arrays), set(directions.ARRAY_FIELDS))
        self.assertTrue(all(a.ndim == 1 for a in record.arrays.values()))

    def test_signed_means_movement_and_present_state(self):
        first = self.record()
        second = directions.accumulate(first, [-1., 0.], [0., 1., 0.], [0., .01, 0.], 1.)
        np.testing.assert_array_equal(first.arrays['current_input'], [1., 0.])
        np.testing.assert_array_equal(second.arrays['input_mean'], [0., 0.])
        np.testing.assert_array_equal(second.arrays['current_input'], [-1., 0.])
        self.assertLess(second.arrays['input_flow'][0], 0)
        self.assertGreater(second.arrays['output_flow'][1], 0)
        self.assertIs(directions.accumulate(second, [9.], [9.], [9.], 0), second)

    def test_role_mass_and_shared_signed_write_coefficient(self):
        c = write_coefficients(np.array([-5., 4., 1.]), np.array([1,1,0]),
                               eta=.1, role_scale=.5, step_fraction_max=.02)
        np.testing.assert_allclose(c, [-.02, .02, 0.])
        first = self.record()
        second = directions.accumulate(first, [-1., 0.], [0., 1., 0.], [0., .01, 0.], 3.)
        np.testing.assert_allclose(second.arrays['input_mean'], [-.5, 0.])

    def test_encrypted_round_trip_and_legacy_checkpoint(self):
        records = {'p':self.record()}
        meta = directions.metadata(records, 4)
        arrays = dict(directions.tensors(records))
        loaded = directions.restore(meta, arrays, {'p':(2,3)}, 4)
        for key in directions.ARRAY_FIELDS:
            np.testing.assert_array_equal(loaded['p'].arrays[key], records['p'].arrays[key])
        self.assertEqual(directions.restore(None, {}, {'p':(2,3)}, 3), {})
        with self.assertRaises(ValueError):directions.restore(meta, arrays, {'p':(2,3)}, 5)
        with self.assertRaises(ValueError):directions.restore(meta, {}, {'p':(2,3)}, 4)
        arrays['direction.p.input_mean'] = np.array([np.nan, 0.], np.float32)
        with self.assertRaises(ValueError):directions.restore(meta, arrays, {'p':(2,3)}, 4)

    def test_snapshot_preserves_directions_without_opening_native_cache(self):
        with tempfile.TemporaryDirectory() as path:
            source, dest = Vault(Path(path)/'source'), Vault(Path(path)/'dest')
            records = {'p':self.record()}
            meta = dict(baseline_hash='base', tensor_map_version=1, kind='complete', config={},
                        directions=directions.metadata(records, 4), cache='private')
            data = [('p.indices',np.array([0],np.uint32)), ('p.codes',np.ones((1,8),np.uint32))]
            data += list(directions.tensors(records)) + [('cache.0',np.ones(3,np.float32))]
            source.save('checkpoint',meta,data)
            cache = next(t for t in source.manifest('checkpoint')['tensors'] if t['name']=='cache.0')
            for chunk in cache['chunks']:(source.root/'checkpoint'/chunk['file']).write_bytes(b'inaccessible cache')
            snapshot(source,'checkpoint',dest)
            self.assertNotIn('cache',dest.manifest('source')['metadata'])
            self.assertEqual(len(list(dest.arrays('source'))),2+len(directions.ARRAY_FIELDS))
            self.assertEqual(len(list(weight_arrays(dest,'source'))),2)
            restored = directions.restore(dest.manifest('source')['metadata']['directions'],
                {k:a for k,a in dest.arrays('source') if k.startswith('direction.')}, {'p':(2,3)},4)
            self.assertEqual(restored['p'].samples,1)
