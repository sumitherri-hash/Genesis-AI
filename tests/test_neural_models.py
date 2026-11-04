import unittest
import neural_models


class NeuralModelRegistryTests(unittest.TestCase):
    def test_catalog_has_requested_architectures(self):
        self.assertEqual(
            set(neural_models.MODEL_CATALOG),
            {"autoencoder", "recurrent_neural_network", "variational_autoencoder"},
        )

    def test_status_is_truthful(self):
        result = neural_models.status()
        self.assertEqual(result["ready"], neural_models.TORCH_AVAILABLE)
        self.assertIn(result["runtime"], {"pytorch", "not_installed"})

    def test_missing_torch_fails_clearly(self):
        if not neural_models.TORCH_AVAILABLE:
            with self.assertRaisesRegex(RuntimeError, "PyTorch is not installed"):
                neural_models.AutoEncoder(4, 2)


if __name__ == "__main__":
    unittest.main()
