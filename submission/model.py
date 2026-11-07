"""
Harness entry point (drop-in replacement for submission-harness/model.py).

To submit, copy into ../quantumrings-challenge-iqc-2026/submission-harness/:
    model.py  (this file)
    qasm_features.py
    artifacts/model.pkl
See install_into_harness.sh.
"""
import math
import os
import sys

import joblib
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))  # repo root, when run from submission/
from qasm_features import featurize as _featurize  # noqa: E402

CAP_SECONDS = 6 * 60 * 60


class RuntimeModel:
    def __init__(self, artifacts_dir=None):
        for d in filter(None, [artifacts_dir, os.path.join(HERE, "artifacts"),
                               os.path.join(os.path.dirname(HERE), "artifacts")]):
            p = os.path.join(d, "model.pkl")
            if os.path.exists(p):
                bundle = joblib.load(p)
                break
        else:
            raise FileNotFoundError("artifacts/model.pkl not found; run train.py first")
        self.model = bundle["model"]
        self.cols = bundle["cols"]

    def featurize(self, qasm_text: str) -> dict:
        return _featurize(qasm_text)

    def predict(self, features: dict, threshold: int) -> float:
        row = dict(features)
        row["log2_threshold"] = math.log2(threshold)
        x = np.array([[float(row.get(c, 0.0)) for c in self.cols]])
        sec = 10 ** float(self.model.predict(x)[0])
        # Anything predicted near the cap is treated as a timeout: predicting
        # >= cap gets full credit on a real timeout.
        if sec >= 0.8 * CAP_SECONDS:
            sec = max(sec, CAP_SECONDS)
        return float(sec)
