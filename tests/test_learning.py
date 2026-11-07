import tempfile
from pathlib import Path
import unittest

import joblib
import numpy as np

from runtime_pipeline.learning import fit_model
from runtime_pipeline.model import RuntimeModel


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.features = [{"n_qubits": float(i), "n_ops": float(i * 10)} for i in range(1, 41)]
        self.thresholds = [16 if i % 2 else 64 for i in range(40)]
        self.targets = [float(i * i + 1) for i in range(40)]

    def test_median_thresholds_and_unseen_fallback(self):
        model = fit_model(self.features, self.thresholds, self.targets, 42, {"kind": "median"})
        predicted = model.predict([{}] * 3, [16, 64, 1024])
        self.assertAlmostEqual(predicted[2], np.median(self.targets))
        self.assertAlmostEqual(predicted[0], np.median(np.array(self.targets)[np.array(self.thresholds) == 16]))

    def test_trees_serialization_and_harness_contract(self):
        model = fit_model(self.features, self.thresholds, self.targets, 42,
                          {"kind": "extra_trees", "n_estimators": 12, "n_jobs": 1})
        predicted = model.predict(self.features, self.thresholds)
        self.assertTrue(np.all(np.isfinite(predicted)))
        self.assertTrue(np.all((predicted > 0) & (predicted <= 21600)))
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            joblib.dump(model, Path(tmp) / "model.joblib")
            adapter = RuntimeModel(tmp)
            features = adapter.featurize("OPENQASM 3.0; qubit a; h a;")
            self.assertIsInstance(adapter.predict(features, 16), float)

    def test_learning_vocabulary_is_training_fold_local(self):
        model = fit_model([{"a": 1}, {"a": 2}], [16, 16], [1, 2], 42,
                          {"kind": "extra_trees", "n_estimators": 3, "n_jobs": 1})
        model.predict([{"a": 100, "validation_only": 1}], [16])
        self.assertNotIn("validation_only", model.model.named_steps["vectorizer"].vocabulary_)

    def test_identity_and_invalid_targets_rejected(self):
        with self.assertRaises(ValueError):
            fit_model([{"filename": 1}], [16], [1], 42, {"kind": "median"})
        with self.assertRaises(ValueError):
            fit_model([{}], [16], [float("nan")], 42, {"kind": "median"})

    def test_hist_gradient_boosting(self):
        model = fit_model(self.features, self.thresholds, self.targets, 43,
                          {"kind": "hist_gradient_boosting", "max_iter": 5})
        self.assertEqual(len(model.predict(self.features, self.thresholds)), 40)


if __name__ == "__main__":
    unittest.main()
