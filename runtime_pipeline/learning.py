"""Training-fold-local preprocessing and bounded CPU regressors."""

from dataclasses import dataclass
import math

import numpy as np
from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor
from sklearn.feature_extraction import DictVectorizer
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from threadpoolctl import threadpool_limits

CAP_SECONDS = 21600.0
MIN_SECONDS = 1e-9


def _rows(feature_rows, thresholds):
    if len(feature_rows) != len(thresholds):
        raise ValueError("Features and thresholds have different lengths")
    result = []
    for features, threshold in zip(feature_rows, thresholds):
        threshold = float(threshold)
        if not math.isfinite(threshold) or threshold <= 0:
            raise ValueError("Threshold must be a positive finite number")
        row = {}
        for key, value in features.items():
            if key.lower() in {"filename", "source", "algorithm", "status", "duration_s", "target", "group", "circuit_id"}:
                raise ValueError(f"Prohibited identity or target feature: {key}")
            if not isinstance(value, (int, float, np.number)):
                raise ValueError(f"Feature {key} must be numeric")
            row[key] = float(value) if math.isfinite(float(value)) else np.nan
        row["threshold"] = threshold
        row["log2_threshold"] = math.log2(threshold)
        result.append(row)
    return result


@dataclass
class FittedRuntimeEstimator:
    model: object | None
    medians: dict[float, float]
    global_median: float
    config: dict

    def predict(self, feature_rows, thresholds):
        rows = _rows(feature_rows, thresholds)
        if not rows:
            return np.empty(0, dtype=float)
        if self.model is None:
            prediction = np.asarray([self.medians.get(float(t), self.global_median) for t in thresholds])
        else:
            with threadpool_limits(limits=max(1, min(8, int(self.config.get("n_jobs", 4))))):
                prediction = np.power(10.0, np.clip(self.model.predict(rows), -9, math.log10(CAP_SECONDS)))
        if not np.all(np.isfinite(prediction)):
            raise ValueError("Estimator produced nonfinite predictions")
        return np.clip(prediction, MIN_SECONDS, CAP_SECONDS)


def fit_model(feature_rows: list[dict], thresholds: list[int], targets_s: list[float],
              seed: int, config: dict) -> FittedRuntimeEstimator:
    if not feature_rows or len(feature_rows) != len(targets_s):
        raise ValueError("Training features and targets must be nonempty and aligned")
    rows = _rows(feature_rows, thresholds)
    targets = np.asarray(targets_s, dtype=float)
    if not np.all(np.isfinite(targets)) or np.any(targets <= 0):
        raise ValueError("Targets must be positive and finite")
    targets = np.clip(targets, MIN_SECONDS, CAP_SECONDS)
    threshold_array = np.asarray(thresholds, dtype=float)
    medians = {float(t): float(np.median(targets[threshold_array == t])) for t in np.unique(threshold_array)}
    kind = config.get("kind", "extra_trees")
    if kind == "median":
        return FittedRuntimeEstimator(None, medians, float(np.median(targets)), dict(config))
    if kind == "extra_trees":
        regressor = ExtraTreesRegressor(
            n_estimators=int(config.get("n_estimators", 400)),
            min_samples_leaf=int(config.get("min_samples_leaf", 2)),
            max_features=config.get("max_features", 1.0),
            max_depth=config.get("max_depth"),
            n_jobs=max(1, min(8, int(config.get("n_jobs", 4)))),
            random_state=seed,
        )
    elif kind == "hist_gradient_boosting":
        regressor = HistGradientBoostingRegressor(
            loss=config.get("loss", "absolute_error"),
            max_iter=int(config.get("max_iter", 250)),
            learning_rate=float(config.get("learning_rate", 0.06)),
            max_leaf_nodes=int(config.get("max_leaf_nodes", 15)),
            min_samples_leaf=int(config.get("min_samples_leaf", 10)),
            l2_regularization=float(config.get("l2_regularization", 1.0)),
            early_stopping=False,
            random_state=seed,
        )
    else:
        raise ValueError(f"Unsupported estimator kind: {kind}")
    model = Pipeline([
        ("vectorizer", DictVectorizer(sparse=False)),
        ("imputer", SimpleImputer(strategy="median", keep_empty_features=True)),
        ("regressor", regressor),
    ])
    with threadpool_limits(limits=max(1, min(8, int(config.get("n_jobs", 4))))):
        model.fit(rows, np.log10(targets))
    return FittedRuntimeEstimator(model, medians, float(np.median(targets)), dict(config))
