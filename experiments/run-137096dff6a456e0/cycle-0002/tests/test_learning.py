"""Synthetic contract checks; reduced forests do not measure candidate performance."""

from itertools import product
import tempfile
from pathlib import Path
import unittest

import joblib
import numpy as np

from runtime_pipeline.learning import fit_model
from model import RuntimeModel
from runtime_pipeline.model import RuntimeModel as PipelineRuntimeModel
from runtime_pipeline.parser import featurize


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.features = [{"n_qubits": float(i), "n_ops": float(i * 10)} for i in range(1, 41)]
        self.thresholds = [16 if i % 2 else 64 for i in range(40)]
        self.targets = [float(i * i + 1) for i in range(40)]

    def assert_bounded_predictions(self, predicted, count):
        self.assertEqual(predicted.shape, (count,))
        self.assertTrue(np.issubdtype(predicted.dtype, np.floating))
        self.assertTrue(np.all(np.isfinite(predicted)))
        self.assertTrue(np.all((predicted >= 1e-9) & (predicted <= 21600)))

    def test_extra_trees_criterion_controls_unsplittable_log_target_prediction(self):
        rows = [{"n_qubits": 2.0, "n_ops": 10.0} for _ in range(3)]
        thresholds = [16, 16, 16]
        targets = [1.0, 1.0, 10000.0]
        # Constant features force one leaf: median(log10 targets) = 0,
        # while mean(log10 targets) = 4/3. This checks predictions in seconds.
        cases = [
            ({}, "squared_error", 10 ** (4 / 3)),
            ({"criterion": "squared_error"}, "squared_error", 10 ** (4 / 3)),
            ({"criterion": "absolute_error"}, "absolute_error", 1.0),
        ]
        for seed, (options, expected_criterion, expected_seconds) in product((42, 43), cases):
            with self.subTest(seed=seed, criterion=options.get("criterion", "omitted")):
                config = {"kind": "extra_trees", "n_estimators": 3,
                          "min_samples_leaf": 2, "n_jobs": 1, **options}
                model = fit_model(rows, thresholds, targets, seed, config)
                regressor = model.model.named_steps["regressor"]
                self.assertEqual(regressor.criterion, expected_criterion)
                self.assertEqual(regressor.random_state, seed)
                for tree in regressor.estimators_:
                    self.assertEqual(tree.criterion, expected_criterion)
                    self.assertEqual(tree.tree_.node_count, 1)
                predicted = model.predict(rows, thresholds)
                self.assert_bounded_predictions(predicted, 3)
                np.testing.assert_allclose(predicted, [expected_seconds] * 3, rtol=1e-12, atol=0)

    def test_median_clips_before_aggregation_and_unseen_fallback(self):
        model = fit_model(
            [{"n_ops": i + 1} for i in range(6)], [16, 16, 64, 64, 256, 256],
            [1e-12, 1e-11, 1.0, 43200.0, 86400.0, 216000.0], 42, {"kind": "median"},
        )
        self.assertEqual(model.medians, {16.0: 1e-9, 64.0: 10800.5, 256.0: 21600.0})
        self.assertEqual(model.global_median, 10800.5)
        fitted_state = joblib.hash(model)
        thresholds = [256, 1024, 16, 64]
        expected = [21600.0, 10800.5, 1e-9, 10800.5]
        for rows in ([{}] * 4, [{"n_ops": 1e20, "held_out_only": i} for i in range(4)]):
            predicted = model.predict(rows, thresholds)
            self.assert_bounded_predictions(predicted, 4)
            np.testing.assert_array_equal(predicted, expected)
        self.assertEqual(joblib.hash(model), fitted_state)

    def test_serialization_and_root_harness_contract(self):
        self.assertIs(RuntimeModel, PipelineRuntimeModel)
        qasm = "OPENQASM 3.0; qubit a; h a;"
        features = featurize(qasm_text=qasm)
        self.assertIsInstance(features, dict)
        self.assertTrue(all(isinstance(value, (int, float, np.number)) for value in features.values()))
        rows = [features, {}, {"n_ops": float("nan"), "held_out_only": 3.0}]
        thresholds = [16, 1024, 64]
        configs = (
            {"kind": "median"},
            {"kind": "extra_trees", "n_estimators": 12, "n_jobs": 1},
            {"kind": "extra_trees", "criterion": "absolute_error", "n_estimators": 12, "n_jobs": 1},
        )
        for config in configs:
            with self.subTest(config=config):
                model = fit_model(self.features, self.thresholds, self.targets, 42, config)
                predicted = model.predict(feature_rows=rows, thresholds=thresholds)
                self.assert_bounded_predictions(predicted, len(rows))
                with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
                    with self.assertRaises(FileNotFoundError):
                        RuntimeModel(artifacts_dir=tmp)
                    artifact = Path(tmp) / "model.joblib"
                    joblib.dump(model, artifact)
                    restored = joblib.load(artifact)
                    np.testing.assert_allclose(restored.predict(rows, thresholds), predicted, rtol=1e-12, atol=0)
                    adapter = RuntimeModel(artifacts_dir=tmp)
                    self.assertEqual(adapter.featurize(qasm_text=qasm), features)
                    for row, threshold, expected in zip(rows, thresholds, predicted):
                        actual = adapter.predict(features=row, threshold=threshold)
                        self.assertIsInstance(actual, float)
                        np.testing.assert_allclose(actual, expected, rtol=1e-12, atol=0)

    def test_training_preprocessing_and_estimator_state_are_fold_local(self):
        rows = [
            {"n_ops": 1.0, "depth_proxy": 1.0, "sample_mean_degree": float("nan")},
            {"n_ops": 3.0, "depth_proxy": float("nan"), "sample_mean_degree": float("inf")},
            {"n_ops": 5.0, "depth_proxy": 9.0, "sample_mean_degree": float("-inf")},
            {"n_ops": 7.0, "depth_proxy": float("inf"), "sample_mean_degree": float("nan")},
            {"n_ops": 99.0, "depth_proxy": 101.0, "sample_mean_degree": float("nan")},
        ]
        thresholds = [16, 16, 16, 64, 64]
        expected_statistics = {
            "n_ops": 5.0, "depth_proxy": 9.0, "sample_mean_degree": 0.0,
            "threshold": 16.0, "log2_threshold": 4.0,
        }
        for criterion, seed in product(("squared_error", "absolute_error"), (42, 43)):
            with self.subTest(criterion=criterion, seed=seed):
                config = {"kind": "extra_trees", "criterion": criterion, "n_estimators": 400,
                          "min_samples_leaf": 2, "max_features": 1.0, "n_jobs": 4}
                model = fit_model(feature_rows=rows, thresholds=thresholds,
                                  targets_s=[1.0, 4.0, 9.0, 16.0, 25.0], seed=seed, config=config)
                vectorizer = model.model.named_steps["vectorizer"]
                imputer = model.model.named_steps["imputer"]
                regressor = model.model.named_steps["regressor"]
                self.assertEqual(set(vectorizer.vocabulary_), set(expected_statistics))
                self.assertEqual(dict(zip(vectorizer.get_feature_names_out(), imputer.statistics_)),
                                 expected_statistics)
                self.assertEqual(regressor.random_state, seed)
                for name in ("criterion", "n_estimators", "min_samples_leaf", "max_features", "n_jobs"):
                    self.assertEqual(getattr(regressor, name), config[name])
                self.assertEqual(len(regressor.estimators_), 400)
                self.assertEqual(model.medians, {16.0: 4.0, 64.0: 20.5})
                self.assertEqual(model.global_median, 9.0)
                # Hash the entire fitted object, including learned tree arrays and preprocessing.
                fitted_state = joblib.hash(model)
                training_predictions = model.predict(rows, thresholds)
                held_out = [
                    {"n_ops": 1e20, "depth_proxy": 1e20, "sample_mean_degree": 1e20,
                     "held_out_only": 1e20},
                    {},
                    {"n_ops": float("nan"), "depth_proxy": float("-inf")},
                ]
                predicted = model.predict(held_out, [16, 1024, 4096])
                self.assert_bounded_predictions(predicted, len(held_out))
                np.testing.assert_allclose(model.predict(rows, thresholds), training_predictions,
                                           rtol=1e-12, atol=0)
                self.assertEqual(joblib.hash(model), fitted_state)

    def test_trees_fit_log10_of_clipped_seconds(self):
        # With two samples and leaf size two, both criteria predict the midpoint
        # of the clipped log runtimes (the mean and median coincide).
        cases = [
            ([1e-12, 1e-11], 1e-9),
            ([1e-12, 216000.0], np.sqrt(1e-9 * 21600.0)),
            ([43200.0, 216000.0], 21600.0),
        ]
        for criterion, (targets, expected) in product(("squared_error", "absolute_error"), cases):
            with self.subTest(criterion=criterion, targets=targets):
                model = fit_model([{"n_ops": 1}, {"n_ops": 2}], [16, 16], targets, 42,
                                  {"kind": "extra_trees", "criterion": criterion, "n_estimators": 3,
                                   "min_samples_leaf": 2, "n_jobs": 1})
                predicted = model.predict([{}, {"n_ops": 1e20}], [16, 1024])
                self.assert_bounded_predictions(predicted, 2)
                np.testing.assert_allclose(predicted, [expected, expected], rtol=1e-12, atol=0)

    def test_prediction_counts_and_alignment(self):
        configs = (
            {"kind": "median"},
            {"kind": "extra_trees", "n_estimators": 3, "n_jobs": 1},
            {"kind": "extra_trees", "criterion": "absolute_error", "n_estimators": 3, "n_jobs": 1},
        )
        for config in configs:
            with self.subTest(config=config):
                model = fit_model(self.features, self.thresholds, self.targets, 42, config)
                self.assert_bounded_predictions(model.predict([], []), 0)
                predicted = model.predict(self.features, self.thresholds)
                self.assert_bounded_predictions(predicted, 40)
                order = [39, 0, 17, 17, 5]
                reordered = model.predict([self.features[i] for i in order],
                                          [self.thresholds[i] for i in order])
                self.assert_bounded_predictions(reordered, len(order))
                np.testing.assert_allclose(reordered, predicted[order], rtol=1e-12, atol=0)
                for rows, thresholds in (([], [16]), ([{}], []), ([{}, {}], [16]), ([{}], [16, 64])):
                    with self.subTest(rows=rows, thresholds=thresholds), self.assertRaises(ValueError):
                        model.predict(rows, thresholds)

    def test_identity_and_invalid_targets_rejected(self):
        configs = (
            {"kind": "median"},
            {"kind": "extra_trees", "n_estimators": 3, "n_jobs": 1},
            {"kind": "extra_trees", "criterion": "absolute_error", "n_estimators": 3, "n_jobs": 1},
        )
        for config in configs:
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    fit_model([{"filename": 1}], [16], [1], 42, config)
                with self.assertRaises(ValueError):
                    fit_model([{}], [16], [float("nan")], 42, config)

    def test_hist_gradient_boosting(self):
        model = fit_model(self.features, self.thresholds, self.targets, 43,
                          {"kind": "hist_gradient_boosting", "max_iter": 5})
        self.assertEqual(len(model.predict(self.features, self.thresholds)), 40)


if __name__ == "__main__":
    unittest.main()
