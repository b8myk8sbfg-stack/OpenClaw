import unittest

from brand_inference import infer_brand_from_part


class InferBrandTests(unittest.TestCase):
    def test_zfc_infers_smc(self):
        self.assertEqual(infer_brand_from_part("ZFC-EL-4"), "SMC")

    def test_zs_infers_smc(self):
        self.assertEqual(infer_brand_from_part("ZS-46-5F"), "SMC")


if __name__ == "__main__":
    unittest.main()
