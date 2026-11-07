"""Train + cross-validate the runtime model.

target  = log10(duration_s); timeout rows -> log10(21600)
inputs  = parsed circuit features + C++ stepper features (st_*) + log2(threshold)
model   = default (--blend): equal-weight log10-space mean of 4 HistGradientBoostingRegressors
          (see MEMBERS), plus a 5th member 'twin_only'; st_* members and twin_only also get the
          structural-twin columns (TWIN_COLS, see submission/model.py TwinIndex; --no-twin disables);
          --no-blend: a single HGB with the --monotone constraint
CV      = GroupKFold by filename (all thresholds of one circuit stay in one fold)
metrics = challenge_score: mean(max(0, 1 - |log10(pred/actual)|)), both clipped at 21600
          official: pred capped at 21600 only on timeout rows, floored at 1e-9

Usage: python train.py --features features.csv --labels ../quantumrings-challenge-iqc-2026/train-runtime-data.csv
"""
import argparse
import importlib.util
import json
from pathlib import Path

import joblib
from joblib import Parallel, delayed
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.model_selection import GroupKFold

_spec = importlib.util.spec_from_file_location(
    "_submission_model", Path(__file__).resolve().parent / "submission" / "model.py")
_submission_model = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_submission_model)
derive = _submission_model.derive  # single source of truth for derived columns
TwinIndex = _submission_model.TwinIndex  # structural-twin lookup, also used by predict()
TWIN_COLS = list(_submission_model.TWIN_COLS)
twin_features = _submission_model.twin_features
motif_vector = _submission_model.motif_vector

CAP = 21600.0
DROP = {"filename", "parse_s", "st_n_gates", "st_n_2q", "st_n_qubits", "st_n_measure",
        "st_n_reset", "st_n_3q_plus"}


def challenge_score(pred_s, actual_s):
    p = np.clip(pred_s, 1e-6, CAP)
    a = np.clip(actual_s, 1e-6, CAP)
    return np.maximum(0.0, 1.0 - np.abs(np.log10(p / a)))


def official_score(pred_s, actual_s, timeout):
    p = np.where(timeout, np.minimum(pred_s, CAP), pred_s)
    p = np.maximum(p, 1e-9)
    return np.maximum(0.0, 1.0 - np.abs(np.log10(p / actual_s)))


# derived per-row columns: computed by submission/model.py derive(), shared with predict()
NORM = ("cost_sel_per_gate", "cost_sel_per_2q", "cost_ep_sel_per_2q")
DERIVED = ("log2_threshold", "cost_sel", "cost_ep_sel", "thr_copy") + NORM
DERIVED_DOC = ("log2_threshold = log2(threshold); thr_copy = log2_threshold; "
               "cost_sel = st_log10_cost_<threshold>; cost_ep_sel = st_log10_cost_ep_<threshold>; "
               "cost_sel_per_gate = cost_sel - log10(1+n_gates); cost_sel_per_2q = cost_sel - log10(1+n_2q); "
               "cost_ep_sel_per_2q = cost_ep_sel - log10(1+n_2q); missing st_* -> 0.0")


def load(features_csv, labels_csv, thr_copy=False):
    feats = pd.read_csv(features_csv)
    lab = pd.read_csv(labels_csv)
    lab["actual_s"] = np.where(lab["status"] == "timeout", CAP, lab["duration_s"])
    df = lab.merge(feats, on="filename", how="inner")
    assert len(df) == len(lab), f"join lost rows: {len(lab)} -> {len(df)}"
    # derived columns come from the SAME derive() that submission/model.py uses at predict time
    der = pd.DataFrame([derive(r, t) for r, t in zip(df.to_dict("records"), df["threshold"])],
                       index=df.index)
    ts = sorted(df["threshold"].astype(int).unique())
    has_cost = all(f"st_log10_cost_{t}" in df for t in ts)
    has_cost_ep = all(f"st_log10_cost_ep_{t}" in df for t in ts)
    drop = set()
    if not thr_copy:  # thr_copy only when log2_threshold is not monotone-only
        drop.add("thr_copy")
    if not has_cost:  # base-only feature file: no stepper cost columns
        drop |= {"cost_sel", "cost_sel_per_gate", "cost_sel_per_2q"}
    if not has_cost_ep:
        drop |= {"cost_ep_sel", "cost_ep_sel_per_2q"}
    for c in DERIVED:  # fixed column order (feature_cols / MEMBERS rely on it)
        if c not in drop:
            df[c] = der[c].to_numpy(float)
    return df


def feature_cols(df, norm=False):
    return [c for c in df.columns
            if c not in DROP and c not in {"shots", "backend", "precision", "threshold",
                                          "duration_s", "status", "actual_s"}
            and (norm or c not in NORM)]


# Blend members: (name, column selector, monotone mode). Selectors are rules, so new
# st_* columns in features.csv are picked up automatically by the st_* members.
def _base_cols(df):  # parsed features + log2_threshold + thr_copy, no stepper info
    return [c for c in feature_cols(df)
            if not c.startswith("st_") and c not in {"cost_sel", "cost_ep_sel"}]


# 4th field: append the structural-twin columns (TWIN_COLS) when twin features are enabled.
MEMBERS = [
    ("base_free", _base_cols, "none", False),
    ("st_none", lambda df: feature_cols(df), "none", True),
    ("st_costsel", lambda df: feature_cols(df), "cost_sel", True),
    ("st_norm", lambda df: feature_cols(df, norm=True), "none", True),
    ("twin_only", _base_cols, "none", True),  # 5th member: parsed features + twin, no stepper
]
BLEND4 = ["base_free", "st_none", "st_costsel", "st_norm"]


MONO = {"both": ("log2_threshold", "cost_sel"), "cost_sel": ("cost_sel",),
        "threshold": ("log2_threshold",), "none": ()}


def make_model(cols, monotone="cost_sel"):
    mono = [1 if c in MONO[monotone] else 0 for c in cols]
    return HistGradientBoostingRegressor(
        max_iter=600, learning_rate=0.04, max_leaf_nodes=15, min_samples_leaf=10,
        l2_regularization=1.0, monotonic_cst=mono, random_state=0)


def _report(name, df, logpred, actual, timeout):
    pred = 10 ** logpred
    ch = challenge_score(pred, actual)
    off = official_score(pred, actual, timeout)
    r = {"cv_score": round(float(ch.mean()), 5), "official_cv_score": round(float(off.mean()), 5),
         "by_threshold": {str(t): round(float(off[df["threshold"].to_numpy() == t].mean()), 5)
                          for t in sorted(df["threshold"].unique())},
         "by_qasm_version": {str(v): round(float(off[df["qasm_version"].to_numpy() == v].mean()), 5)
                             for v in sorted(df["qasm_version"].unique())},
         "timeouts": {"n": int(timeout.sum()), "official": round(float(off[timeout].mean()), 5),
                      "pred_ge_cap": int((pred[timeout] >= CAP).sum())}}
    print(f"{name:12s} challenge {r['cv_score']:.4f}  official {r['official_cv_score']:.4f}  "
          f"timeouts {r['timeouts']['official']:.3f} ({r['timeouts']['pred_ge_cap']}/{r['timeouts']['n']} >= cap)")
    print("   by threshold:", r["by_threshold"], " by version:", r["by_qasm_version"])
    return r, ch, off


# ---------------------------------------------------------------- structural twin
def circuit_table(df):
    """one row per circuit: filename, motif (or None), log10 n_qubits / n_gates, Y[thresholds]."""
    ts = sorted(int(t) for t in df["threshold"].unique())
    first = df.drop_duplicates("filename").set_index("filename")
    Y = (np.log10(df.pivot_table(index="filename", columns="threshold", values="actual_s",
                                 aggfunc="mean")).reindex(index=first.index, columns=ts))
    recs = first.to_dict("index")
    mot = {f: motif_vector(r) for f, r in recs.items()}
    return {"ts": ts, "files": list(first.index), "motif": mot, "Y": Y,
            "lnq": np.log10(first["n_qubits"].clip(lower=1).astype(float)),
            "lng": np.log10(first["n_gates"].clip(lower=1).astype(float)), "recs": recs}


def build_index(ct, files):
    """TwinIndex over `files` with a usable motif vector (zero-motif fallback circuits excluded)."""
    files = [f for f in files if ct["motif"][f] is not None]
    return TwinIndex(files, np.array([ct["motif"][f] for f in files]),
                     ct["lnq"].loc[files].to_numpy(), ct["lng"].loc[files].to_numpy(),
                     ct["ts"], ct["Y"].loc[files].to_numpy())


def twin_block(index, ct, df, rows, loo, means=None):
    """twin feature matrix for df.iloc[rows]; loo=True excludes each row's own circuit.
    Zero-motif rows get `means` (computed here from the valid rows when None)."""
    F = np.full((len(rows), len(TWIN_COLS)), np.nan)
    fn = df["filename"].to_numpy()[rows]
    th = df["threshold"].to_numpy()[rows]
    cache = {}
    for k, (f, t) in enumerate(zip(fn, th)):
        if ct["motif"][f] is None:
            continue
        if (f, t) not in cache:
            r = twin_features(index, {}, ct["recs"][f], t, exclude_filename=f if loo else None)
            cache[f, t] = [r[c] for c in TWIN_COLS]
        F[k] = cache[f, t]
    valid = ~np.isnan(F).any(axis=1)
    if means is None:
        means = {c: float(F[valid, i].mean()) for i, c in enumerate(TWIN_COLS)}
    F[~valid] = [means[c] for c in TWIN_COLS]
    return F, means


def resolve_members(df, twin):
    members = []
    for name, sel, mono, use_twin in MEMBERS:
        cols = sel(df)
        if not all(c in df for c in MONO[mono]):
            print(f"skipping member {name}: monotone column(s) {MONO[mono]} missing")
            continue
        if name not in ("base_free", "twin_only") and not any(c.startswith("st_") for c in cols):
            print(f"skipping member {name}: no st_* columns in features")
            continue
        if use_twin and not twin:
            if name == "twin_only":
                continue
            use_twin = False
        members.append((name, cols, mono, use_twin))
        if twin and use_twin and name != "twin_only":  # no-twin twin of each st member, for comparison
            members.append((name + "_nt", cols, mono, False))
    return members


def _fit_predict(allc, mono, Xtr, ytr, Xte):
    from threadpoolctl import threadpool_limits
    with threadpool_limits(1):  # one thread per HGB: much faster than OpenMP on these sizes
        return make_model(allc, mono).fit(Xtr, ytr).predict(Xte)


def run_cv(df, y, folds, members, twin, ct, jobs=8):
    """OOF log10 predictions per member; twin index rebuilt per fold from training circuits only."""
    oof = {n: np.zeros(len(df)) for n, _, _, _ in members}
    oof_twin = np.full((len(df), len(TWIN_COLS)), np.nan)
    oof_triv = np.zeros(len(df))
    fn = df["filename"].to_numpy()
    tasks = []
    for tr, te in folds:
        if twin:
            index = build_index(ct, sorted(set(fn[tr])))
            # sanity (a): no test-fold circuit is ever in the index that answers its query
            assert set(fn[te]).isdisjoint(index.filenames), "test-fold filename leaked into twin index"
            Ftr, means = twin_block(index, ct, df, tr, loo=True)
            Fte, _ = twin_block(index, ct, df, te, loo=False, means=means)
            oof_twin[te] = Fte
        for name, cols, mono, use_twin in members:
            X = df[cols].to_numpy(float)
            Xtr, Xte = X[tr], X[te]
            if use_twin:
                Xtr, Xte = np.c_[Xtr, Ftr], np.c_[Xte, Fte]
            tasks.append((name, te, (cols + (TWIN_COLS if use_twin else []), mono, Xtr, y[tr], Xte)))
        med = df.iloc[tr].groupby("threshold")["actual_s"].median()
        oof_triv[te] = df.iloc[te]["threshold"].map(med).to_numpy()
    preds = Parallel(n_jobs=jobs)(delayed(_fit_predict)(*t[2]) for t in tasks)
    for (name, te, _), p in zip(tasks, preds):
        oof[name][te] = p
    return oof, oof_twin, oof_triv


def blends(oof, members, twin):
    names = [n for n, _, _, _ in members]
    b = {"blend4": np.mean([oof[n] for n in BLEND4 if n in names], axis=0)}
    if twin:
        b["blend5"] = np.mean([oof[n] for n in BLEND4 + ["twin_only"]], axis=0)
        b["blend4_notwin"] = np.mean([oof["base_free"]] + [oof[n + "_nt"] for n in BLEND4[1:]], axis=0)
    return b


def load_splits(path, df):
    sp = pd.read_csv(path)
    cols = [c for c in sp.columns if c.startswith("fold_")]
    m = df[["filename", "threshold"]].merge(sp, on=["filename", "threshold"], how="left")
    assert len(m) == len(df) and not m[cols].isna().any().any(), "splits file does not cover all rows"
    out = {}
    for c in cols:
        assert (m.groupby("filename")[c].nunique() == 1).all(), f"{c} splits a circuit across folds"
        f = m[c].to_numpy()
        out[c] = [(np.where(f != k)[0], np.where(f == k)[0]) for k in sorted(np.unique(f))]
    return out


def main_blend(a):
    df = load(a.features, a.labels, thr_copy=True)
    y = np.log10(df["actual_s"].to_numpy())
    actual = df["actual_s"].to_numpy()
    timeout = (df["status"] == "timeout").to_numpy()
    groups = df["filename"].to_numpy()
    twin = a.twin and all(f"st_motif_{i}" in df for i in range(64))
    if a.twin and not twin:
        print("twin features disabled: st_motif_0..63 not in the feature file")
    ct = circuit_table(df) if twin else None
    members = resolve_members(df, twin)
    print("members:", [(n, len(c) + (len(TWIN_COLS) if t else 0), m) for n, c, m, t in members])

    folds = list(GroupKFold(a.folds).split(df, y, groups))
    oof, oof_twin, oof_triv = run_cv(df, y, folds, members, twin, ct, a.jobs)
    bl = blends(oof, members, twin)

    triv = challenge_score(oof_triv, actual).mean()
    print(f"trivial median-per-threshold CV score: {triv:.3f}")
    print(f"{a.folds}-fold GroupKFold CV:")
    report = {}
    for name, _, _, _ in members:
        report[name], _, _ = _report(name, df, oof[name], actual, timeout)
    scores = {}
    for name, b in bl.items():
        report[name], ch_b, off_b = _report(name, df, b, actual, timeout)
        scores[name] = (ch_b, off_b)

    twin_report = {}
    if twin:  # sanity (b): is the gain only from near-duplicates?
        cos = oof_twin[:, TWIN_COLS.index("twin_cos")]
        valid = np.array([ct["motif"][f] is not None for f in df["filename"]])
        lo = valid & (cos < 0.9)
        twin_report = {"rows_with_motif": int(valid.sum()),
                       "frac_twin_cos_gt_0.99": round(float((cos[valid] > 0.99).mean()), 4),
                       "frac_twin_cos_lt_0.9": round(float((cos[valid] < 0.9).mean()), 4),
                       "n_twin_cos_lt_0.9": int(lo.sum()), "subset_cos_lt_0.9": {},
                       "subset_cos_gt_0.99": {}}
        hi = valid & (cos > 0.99)
        print(f"twin_cos (OOF, rows with motif {valid.sum()}): >0.99 {twin_report['frac_twin_cos_gt_0.99']:.3f}, "
              f"<0.9 {twin_report['frac_twin_cos_lt_0.9']:.3f}; quantiles "
              f"{np.round(np.quantile(cos[valid], [0.1, 0.25, 0.5, 0.75, 0.9]), 3).tolist()}")
        for name, (ch_b, off_b) in scores.items():
            twin_report["subset_cos_lt_0.9"][name] = round(float(off_b[lo].mean()), 5)
            twin_report["subset_cos_gt_0.99"][name] = round(float(off_b[hi].mean()), 5)
        print("   official on rows twin_cos < 0.9 :", twin_report["subset_cos_lt_0.9"])
        print("   official on rows twin_cos > 0.99:", twin_report["subset_cos_gt_0.99"])

    split_report = {}
    if a.splits:
        print(f"--splits {a.splits}:")
        for sname, sfolds in load_splits(a.splits, df).items():
            o, _, _ = run_cv(df, y, sfolds, members, twin, ct, a.jobs)
            split_report[sname] = {}
            for name, b in {**{n: o[n] for n, _, _, _ in members}, **blends(o, members, twin)}.items():
                p = 10 ** b
                split_report[sname][name] = {"challenge": round(float(challenge_score(p, actual).mean()), 5),
                                             "official": round(float(official_score(p, actual, timeout).mean()), 5)}
            print(f"  {sname:9s}", {k: v["official"] for k, v in split_report[sname].items()})
        names = list(next(iter(split_report.values())))
        split_report["mean4"] = {n: {m: round(float(np.mean([split_report[s][n][m] for s in split_report])), 5)
                                     for m in ("challenge", "official")} for n in names}
        print("  mean4    ", {k: v["official"] for k, v in split_report["mean4"].items()})

    # shipped blend: --ship, or (auto) the best det-CV official among blend4_notwin / blend4 / blend5
    cands = [n for n in ("blend4_notwin", "blend4", "blend5") if n in report] if twin else ["blend4"]
    ship_name = a.ship if a.ship != "auto" else max(cands, key=lambda n: report[n]["official_cv_score"])
    assert ship_name in cands, f"--ship {ship_name} not available (twin={twin})"
    if ship_name == "blend4_notwin":
        shipped = [m for m in members if m[0] == "base_free"] + \
                  [(n, c, mo, False) for n, c, mo, t in members if n in BLEND4[1:]]
    else:
        ship = BLEND4 + (["twin_only"] if ship_name == "blend5" else [])
        shipped = [m for m in members if m[0] in ship]
    print(f"shipping {ship_name}: {[(m[0], m[3]) for m in shipped]}")

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    twin_bundle = None
    if any(m[3] for m in shipped):  # final index: all circuits; training rows get leave-one-out twin features
        index = build_index(ct, sorted(set(groups)))
        Fall, means = twin_block(index, ct, df, np.arange(len(df)), loo=True)
        twin_bundle = {"index": index.to_state(), "means": means, "cols": TWIN_COLS, "k": 5}
    fitted = []
    for name, cols, mono, use_twin in shipped:
        X = df[cols].to_numpy(float)
        if use_twin:
            X = np.c_[X, Fall]
        allc = cols + (TWIN_COLS if use_twin else [])
        from threadpoolctl import threadpool_limits
        with threadpool_limits(1):
            m = make_model(allc, mono).fit(X, y)
        fitted.append({"name": name, "cols": allc, "monotone": mono, "model": m})
    joblib.dump({"kind": "blend", "members": fitted, "derived": DERIVED_DOC, "twin": twin_bundle,
                 "combine": "mean of member log10 predictions; seconds = 10**mean"}, out / "model.pkl")
    (out / "cv_report.json").write_text(json.dumps({
        "kind": "blend", "shipped": ship_name, "twin": bool(twin),
        "cv_score": report[ship_name]["cv_score"],
        "official_cv_score": report[ship_name]["official_cv_score"],
        "trivial_cv_score": round(float(triv), 4), "folds": a.folds,
        "n_rows": int(len(df)), "n_circuits": int(df["filename"].nunique()),
        "report": report, "twin_cos_analysis": twin_report, "splits": split_report,
        "members": [{"name": n, "monotone": m, "twin": t, "n_features": len(c) + (len(TWIN_COLS) if t else 0),
                     "features": c + (TWIN_COLS if t else [])} for n, c, m, t in members]}, indent=2))
    o = df[["filename", "threshold", "status", "actual_s"]].copy()
    for name, _, _, _ in members:
        o[f"pred_{name}"] = 10 ** oof[name]
    for name, b in bl.items():
        o[f"pred_{name}"] = 10 ** b
    if twin:
        for i, c in enumerate(TWIN_COLS):
            o[c] = oof_twin[:, i]
    o["pred_s"] = 10 ** bl[ship_name]
    o["score"], o["official_score"] = scores[ship_name]
    o.to_csv(out / "oof_predictions.csv", index=False)
    print(f"saved {out/'model.pkl'} ({ship_name}, {len(fitted)} members, twin index in bundle={twin_bundle is not None})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--features", default="features.csv")
    ap.add_argument("--labels", default="../quantumrings-challenge-iqc-2026/train-runtime-data.csv")
    ap.add_argument("--out", default="artifacts")
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--blend", action=argparse.BooleanOptionalAction, default=True,
                    help="train the 4-member blend (default); --no-blend trains one model with --monotone")
    ap.add_argument("--monotone", choices=sorted(MONO), default="cost_sel",
                    help="single-model mode: which of log2_threshold / cost_sel get a +1 monotone constraint")
    ap.add_argument("--twin", action=argparse.BooleanOptionalAction, default=True,
                    help="structural-twin features (needs st_motif_* columns); --no-twin disables")
    ap.add_argument("--ship", choices=["auto", "blend4_notwin", "blend4", "blend5"], default="auto",
                    help="which blend goes into model.pkl (auto = best det-CV official score)")
    ap.add_argument("--jobs", type=int, default=8, help="parallel HGB fits in CV")
    ap.add_argument("--splits", default=None,
                    help="CSV with filename,threshold,fold_* columns: also report CV on each split + mean")
    a = ap.parse_args()
    if a.blend:
        return main_blend(a)
    df = load(a.features, a.labels, thr_copy=a.monotone not in ("both", "threshold"))
    cols = feature_cols(df)
    X = df[cols].to_numpy(float)
    y = np.log10(df["actual_s"].to_numpy())
    groups = df["filename"].to_numpy()

    # trivial reference: median runtime per threshold (README says 28.4% CV)
    oof = np.zeros(len(df))
    oof_triv = np.zeros(len(df))
    for tr, te in GroupKFold(a.folds).split(X, y, groups):
        m = make_model(cols, a.monotone).fit(X[tr], y[tr])
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
    final = make_model(cols, a.monotone).fit(X, y)
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
