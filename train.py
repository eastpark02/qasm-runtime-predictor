"""Train + cross-validate the classical baseline.

target  = log10(duration_s); timeout rows -> log10(21600)
inputs  = parsed circuit features + log2(threshold)
model   = sklearn HistGradientBoostingRegressor, monotone increasing in threshold
CV      = GroupKFold by filename (all thresholds of one circuit stay in one fold)
metric  = challenge score: mean(max(0, 1 - |log10(pred/actual)|)), both clipped at 21600

Usage: python train.py --features features.csv --labels ../quantumrings-challenge-iqc-2026/train-runtime-data.csv
"""
import argparse
import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.model_selection import GroupKFold

CAP = 21600.0
DROP = {"filename", "parse_s"}


def challenge_score(pred_s, actual_s):
    p = np.clip(pred_s, 1e-6, CAP)
    a = np.clip(actual_s, 1e-6, CAP)
    return np.maximum(0.0, 1.0 - np.abs(np.log10(p / a)))


def load(features_csv, labels_csv):
    feats = pd.read_csv(features_csv)
    lab = pd.read_csv(labels_csv)
    lab["actual_s"] = np.where(lab["status"] == "timeout", CAP, lab["duration_s"])
    df = lab.merge(feats, on="filename", how="inner")
    assert len(df) == len(lab), f"join lost rows: {len(lab)} -> {len(df)}"
    df["log2_threshold"] = np.log2(df["threshold"])
    return df


def feature_cols(df):
    return [c for c in df.columns
            if c not in DROP and c not in {"shots", "backend", "precision", "threshold",
                                          "duration_s", "status", "actual_s"}]


def make_model(cols):
    mono = [1 if c == "log2_threshold" else 0 for c in cols]
    return HistGradientBoostingRegressor(
        max_iter=600, learning_rate=0.04, max_leaf_nodes=15, min_samples_leaf=10,
        l2_regularization=1.0, monotonic_cst=mono, random_state=0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--features", default="features.csv")
    ap.add_argument("--labels", default="../quantumrings-challenge-iqc-2026/train-runtime-data.csv")
    ap.add_argument("--out", default="artifacts")
    ap.add_argument("--folds", type=int, default=5)
    a = ap.parse_args()

    df = load(a.features, a.labels)
    cols = feature_cols(df)
    X = df[cols].to_numpy(float)
    y = np.log10(df["actual_s"].to_numpy())
    groups = df["filename"].to_numpy()

    # trivial reference: median runtime per threshold (README says 28.4% CV)
    oof = np.zeros(len(df))
    oof_triv = np.zeros(len(df))
    for tr, te in GroupKFold(a.folds).split(X, y, groups):
        m = make_model(cols).fit(X[tr], y[tr])
        oof[te] = m.predict(X[te])
        med = df.iloc[tr].groupby("threshold")["actual_s"].median()
        oof_triv[te] = df.iloc[te]["threshold"].map(med).to_numpy()

    df["pred_s"] = 10 ** oof
    df["score"] = challenge_score(df["pred_s"].to_numpy(), df["actual_s"].to_numpy())
    triv = challenge_score(oof_triv, df["actual_s"].to_numpy()).mean()
    print(f"trivial median-per-threshold CV score: {triv:.3f}")
    print(f"HGB baseline CV score ({a.folds}-fold GroupKFold): {df['score'].mean():.3f}")
    print("by threshold:\n", df.groupby("threshold")["score"].mean().round(3).to_string())
    print("by qasm version:\n", df.groupby("qasm_version")["score"].mean().round(3).to_string())
    to = df["status"] == "timeout"
    print(f"timeouts: {to.sum()} rows, mean score {df.loc[to,'score'].mean():.3f}, "
          f"predicted >= cap: {(df.loc[to,'pred_s'] >= CAP).sum()}")

    # permutation-free quick importance: drop-column is too slow; report top split features via refit
    final = make_model(cols).fit(X, y)
    out = Path(a.out)
    out.mkdir(exist_ok=True)
    joblib.dump({"model": final, "cols": cols}, out / "model.pkl")
    (out / "cv_report.json").write_text(json.dumps({
        "cv_score": round(float(df["score"].mean()), 4),
        "trivial_cv_score": round(float(triv), 4),
        "n_rows": int(len(df)), "n_circuits": int(df["filename"].nunique()),
        "features": cols}, indent=2))
    df[["filename", "threshold", "status", "actual_s", "pred_s", "score"]].to_csv(out / "oof_predictions.csv", index=False)
    print(f"saved {out/'model.pkl'} ({len(cols)} features)")


if __name__ == "__main__":
    main()
