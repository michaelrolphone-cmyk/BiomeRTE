"""The retired vocabulary/replay route must never be a reverse fallback."""
import unittest
from unittest.mock import Mock
from activation_field.inverse_runtime import reconstruct
from activation_field.reverse import ReverseUnavailable


class ReverseRuntime(unittest.TestCase):
    def source(self, directions=None):
        meta = dict(config={}, baseline_hash='base', tensor_map_version=1, kind='field_only')
        if directions is not None:
            meta['directions'] = directions
        vault = Mock()
        vault.manifest.return_value = {'metadata': meta}
        return vault

    def test_legacy_checkpoint_does_not_start_a_dictionary_fallback(self):
        vault = self.source()
        with self.assertRaises(ReverseUnavailable) as error:
            reconstruct({'baseline_hash':'base'}, vault, 'source')
        self.assertEqual(error.exception.code, 'direction_state_missing')
        vault.arrays.assert_not_called()

    def test_missing_operation_boundaries_are_explicit(self):
        vault = self.source(dict(schema_version=1, coverage='quantized_projections',
                                 projections={'p':dict(mass=1, samples=1)}))
        with self.assertRaises(ReverseUnavailable) as error:
            reconstruct({'baseline_hash':'base'}, vault, 'source')
        self.assertEqual(error.exception.code, 'operation_state_missing')
        vault.arrays.assert_not_called()

    def test_removed_options_fail_before_reading_source(self):
        for options in ({'mode':'first_pass'}, {'mode':'replay'}, {'max_tokens':8}, {'candidate_pool':16}):
            vault = self.source()
            with self.assertRaises(ValueError):
                reconstruct({'baseline_hash':'base'}, vault, 'source', options)
            vault.manifest.assert_not_called()

    def test_cancel_before_loading_source(self):
        vault = self.source()
        def cancel():raise InterruptedError('cancelled')
        with self.assertRaises(InterruptedError):
            reconstruct({'baseline_hash':'base'}, vault, 'source', check=cancel)
        vault.manifest.assert_not_called()
