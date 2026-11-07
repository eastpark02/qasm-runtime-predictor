"""Timing evidence integrity checks; synthetic fits do not certify latency."""

import copy
import unittest

from runtime_pipeline.learning import fit_model
from validation import measure_candidate as candidate


class CandidateTimingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.estimator = fit_model([{"n_ops": i} for i in range(6)], [16] * 6,
                                  [1, 2, 3, 4, 5, 6], 42, candidate.FIXED_CONFIG)

    def test_fitted_settings_and_full_training_count(self):
        result = candidate.verify_fitted(self.estimator, 42, 6)
        self.assertEqual(result["fitted_criterion"], "absolute_error")
        self.assertEqual(result["fitted_tree_count"], 400)
        self.assertEqual(result["fitted_tree_root_row_counts"], [6])
        self.assertEqual(result["fitted_feature_count"], 3)
        for seed, rows in ((43, 6), (42, 7)):
            with self.subTest(seed=seed, rows=rows), self.assertRaises(ValueError):
                candidate.verify_fitted(self.estimator, seed, rows)

    def test_config_cannot_mask_wrong_fitted_settings(self):
        for setting, value in (("criterion", "squared_error"), ("n_jobs", 1),
                               ("max_depth", 3), ("bootstrap", True)):
            with self.subTest(setting=setting):
                model = copy.deepcopy(self.estimator)
                setattr(model.model.named_steps["regressor"], setting, value)
                with self.assertRaises(ValueError):
                    candidate.verify_fitted(model, 42, 6)
        model = copy.deepcopy(self.estimator)
        model.model.named_steps["regressor"].estimators_[0].criterion = "squared_error"
        with self.assertRaises(ValueError):
            candidate.verify_fitted(model, 42, 6)
        model.model.named_steps["regressor"].estimators_.pop(0)
        with self.assertRaises(ValueError):
            candidate.verify_fitted(model, 42, 6)

    def test_interruption_accounts_for_every_unattempted_call(self):
        specs = [{**s, "source": "synthetic"} for s in candidate.helpers.synthetic_specs()]
        self.assertEqual(len(specs), 13)
        specs += [{"name": f"designated-{i}"} for i in range(8)]
        report = {"benchmarks": [], "training": {"parses": []}}
        counts = candidate.account(report, specs, [16, 64, 256, 512, 1024])
        self.assertEqual(counts["parser_calls_not_attempted"], 63)
        self.assertEqual(counts["predictions_not_attempted"], 630)
        self.assertEqual(counts["predictions_ok"], 0)
        self.assertEqual(candidate.account(report, specs, [16, 64, 256, 512, 1024]), counts)

    def test_partial_trial_preserves_exception_and_completes_accounting(self):
        report = {"training": {"parses": []}, "benchmarks": [{"name": "case", "trials": [
            {"repeat": 1, "parser_status": "ok", "parser_s": 0.1, "predictions": [
                {"seed": 42, "threshold": 16, "status": "failed", "elapsed_s": 0.2,
                 "first_call_for_seed": True}]}]}]}
        counts = candidate.account(report, [{"name": "case"}], [16])
        self.assertEqual(counts["predictions_attempted"], 1)
        self.assertEqual(counts["predictions_not_attempted"], 5)
        self.assertEqual(counts["parser_calls_not_attempted"], 2)
        self.assertEqual(report["benchmarks"][0]["trials"][0]["predictions"][0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
