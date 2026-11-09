"""Photometric contract checks; feature count never substitutes for localization."""
import unittest
import numpy as np
from image_preprocessing import preprocessing_policy, preprocess_slam_image


class PreprocessingTests(unittest.TestCase):
    def test_default_preserves_raw_bytes_and_object(self):
        image = np.arange(360, dtype=np.uint8).reshape(10,12,3)
        original = image.copy()
        output, info = preprocess_slam_image(image, preprocessing_policy())
        self.assertIs(output, image)
        np.testing.assert_array_equal(image, original)
        self.assertEqual(info["state"], "raw")

    def test_dark_image_stretch_does_not_modify_original_or_coordinates(self):
        gray = np.tile(np.arange(19,38,dtype=np.uint8), (20,1))
        image = np.repeat(gray[:,:,None],3,axis=2)
        original = image.copy()
        policy = preprocessing_policy({"mode":"percentile_gray"})
        output, info = preprocess_slam_image(image, policy)
        np.testing.assert_array_equal(image, original)
        self.assertEqual(output.shape, image.shape)
        self.assertEqual(output.dtype, np.uint8)
        self.assertFalse(np.shares_memory(output,image))
        self.assertEqual((int(output.min()),int(output.max())), (0,255))
        np.testing.assert_array_equal(output[:,:,0],output[:,:,2])
        self.assertEqual(info["raw_diagnostics"]["minimum"],19)
        self.assertEqual(info["raw_diagnostics"]["maximum"],37)
        self.assertEqual(info["state"],"normalized")
        self.assertIn("not_optical",info["quality_claim"])

    def test_flat_image_does_not_create_contrast_or_nonfinite_values(self):
        image = np.full((20,30,3),24,np.uint8)
        output, info = preprocess_slam_image(image, preprocessing_policy({"mode":"percentile_gray"}))
        np.testing.assert_array_equal(output,image)
        self.assertEqual(info["state"],"flat_range")
        self.assertEqual(info["gain"],1.0)

    def test_invalid_policy_is_rejected(self):
        for value in [False, "percentile_gray", {"mode":"unknown"}, {"version":True},
                      {"mode":"percentile_gray","low_percentile":True},
                      {"mode":"percentile_gray","low_percentile":float("nan")},
                      {"mode":"percentile_gray","high_percentile":101},
                      {"mode":"percentile_gray","low_percentile":99,"high_percentile":1},
                      {"mode":"none","high_percentile":99}, {"extra":1}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                preprocessing_policy(value)

    def test_policy_is_detached_from_mutable_configuration(self):
        value={"mode":"percentile_gray","low_percentile":2}
        policy=preprocessing_policy(value)
        value["low_percentile"]=25
        self.assertEqual(policy["low_percentile"],2.0)
