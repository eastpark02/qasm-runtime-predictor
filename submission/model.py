"""
Harness entry point (drop-in replacement for submission-harness/model.py).

To submit, copy into ../quantumrings-challenge-iqc-2026/submission-harness/:
    model.py  (this file)
    qasm_features.py
    qasm_step.py, qasm_step_py.py
    qasm_step (or qasm_step.exe), built by stepper/build.sh or stepper/build.ps1
    artifacts/model.pkl
See install_into_harness.sh.
"""
import math
import os
import sys
import time

import joblib
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))  # repo root, when run from submission/
from qasm_features import featurize as _featurize  # noqa: E402
from qasm_step import find_binary, finish_step, start_step, step_features  # noqa: E402

_SMOKE_QASM = 'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[2];\nh q[0];\ncx q[0],q[1];\n'

CAP_SECONDS = 6 * 60 * 60
TIMEOUT_RULE_FRAC = 0.6  # any member >= this fraction of the cap -> predict the cap


def derive(row: dict, threshold) -> dict:
    """Per-(circuit, threshold) derived columns. Single source of truth: train.py's load()
    calls this same function for every training row. Missing inputs default to 0.0."""
    t = int(threshold)
    l2 = math.log2(t)
    cost = float(row.get("st_log10_cost_%d" % t, 0.0))
    cost_ep = float(row.get("st_log10_cost_ep_%d" % t, 0.0))
    n_gates = float(row.get("n_gates", 0.0))
    n_2q = float(row.get("n_2q", 0.0))
    return {
        "log2_threshold": l2,
        "thr_copy": l2,  # unconstrained copy of the threshold
        "cost_sel": cost,  # simulated MPS cost at this threshold
        "cost_ep_sel": cost_ep,
        "cost_sel_per_gate": cost - math.log10(1 + n_gates),
        "cost_sel_per_2q": cost - math.log10(1 + n_2q),
        "cost_ep_sel_per_2q": cost_ep - math.log10(1 + n_2q),
    }


N_MOTIF = 64
TWIN_COLS = ("twin_log_t_adj", "twin_log_t_raw", "twin_cos", "twin_dsize", "twin_cos2",
             "twin_mean3_adj")


def motif_vector(row: dict):
    """st_motif_0..63 as a float array, or None when absent/all-zero (stepper fallback path)."""
    v = np.array([float(row.get("st_motif_%d" % i, 0.0) or 0.0) for i in range(N_MOTIF)])
    if not np.all(np.isfinite(v)):
        return None
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else None


class TwinIndex:
    """Structural-twin lookup over the training circuits (stdlib + numpy only).

    For a query circuit: take the k training circuits with the highest cosine similarity of
    the L2-normalised 64-dim motif vector, rank them by Euclidean distance in
    (log10 n_qubits, log10 n_gates), and predict
        twin_log_t_adj = y_twin(T) + slope_T * (log10 n_gates - log10 n_gates_twin)
    with slope_T a global per-threshold least-squares slope of y(T) on log10 n_gates.
    y = log10 runtime, timeouts at log10(21600) (the training target).
    Missing label at T: use the twin's nearest labelled threshold T' (by |log2 T - log2 T'|,
    ties -> lower T') plus the global mean difference mean(y(T) - y(T')) over circuits with
    both labels; if the twin has no label at all, move to the next candidate.
    """

    def __init__(self, filenames, motifs, log_nq, log_ng, thresholds, Y):
        self.filenames = list(filenames)
        U = np.asarray(motifs, float)
        n = np.linalg.norm(U, axis=1, keepdims=True)
        n[n == 0] = 1.0
        self.U = U / n
        self.S = np.c_[np.asarray(log_nq, float), np.asarray(log_ng, float)]
        self.thresholds = [int(t) for t in thresholds]
        self.Y = np.asarray(Y, float).reshape(len(self.filenames), len(self.thresholds))
        self._pos = {f: i for i, f in enumerate(self.filenames)}
        self.slopes = None
        self.mean_diff = None
        self.fit_slopes()

    def fit_slopes(self):
        lng = self.S[:, 1]
        nt = len(self.thresholds)
        self.slopes = np.zeros(nt)
        for j in range(nt):
            ok = ~np.isnan(self.Y[:, j])
            if ok.sum() >= 3 and np.ptp(lng[ok]) > 0:
                self.slopes[j] = np.polyfit(lng[ok], self.Y[ok, j], 1)[0]
        self.mean_diff = np.zeros((nt, nt))  # mean_diff[j, j2] = mean(y_j - y_j2)
        for j in range(nt):
            for j2 in range(nt):
                ok = ~np.isnan(self.Y[:, j]) & ~np.isnan(self.Y[:, j2])
                self.mean_diff[j, j2] = float(np.mean(self.Y[ok, j] - self.Y[ok, j2])) if ok.any() else 0.0
        return self

    def _tidx(self, threshold):
        l2 = [math.log2(t) for t in self.thresholds]
        x = math.log2(max(int(threshold), 1))
        return min(range(len(l2)), key=lambda j: (abs(l2[j] - x), j))

    def _label(self, i, j):
        """twin's log10 runtime at threshold index j (raw, possibly from a neighbouring threshold)."""
        y = self.Y[i, j]
        if not np.isnan(y):
            return float(y)
        l2 = [math.log2(t) for t in self.thresholds]
        order = sorted((j2 for j2 in range(len(l2)) if not np.isnan(self.Y[i, j2])),
                       key=lambda j2: (abs(l2[j2] - l2[j]), j2))
        if not order:
            return None
        return float(self.Y[i, order[0]] + self.mean_diff[j, order[0]])

    def query(self, motif, log_nq, log_ng, threshold, exclude_filename=None, k=5):
        u = np.asarray(motif, float)
        n = float(np.linalg.norm(u))
        u = u / n if n > 0 else u
        cos = self.U @ u
        if exclude_filename is not None and exclude_filename in self._pos:
            cos = cos.copy()
            cos[self._pos[exclude_filename]] = -np.inf
        order = np.argsort(-cos, kind="stable")
        if exclude_filename is not None and exclude_filename in self._pos:
            order = order[:-1]  # the excluded circuit sorts last
        j = self._tidx(threshold)
        slope = self.slopes[j]
        s = np.array([log_nq, log_ng], float)
        # candidates: top-k by cosine that have a usable label; widen past k if needed
        cands = []
        for i in order:
            y = self._label(i, j)
            if y is not None:
                cands.append((float(np.linalg.norm(self.S[i] - s)), int(i), y))
            if len(cands) >= k:
                break
        if not cands:
            return None
        cos2 = float(cos[order[1]]) if len(order) > 1 else float(cos[order[0]])
        cands.sort(key=lambda c: (c[0], -cos[c[1]]))
        adj = [y + slope * (log_ng - self.S[i, 1]) for _, i, y in cands]
        d0, i0, y0 = cands[0]
        return {"twin_log_t_adj": float(adj[0]), "twin_log_t_raw": float(y0),
                "twin_cos": float(cos[i0]), "twin_dsize": float(d0), "twin_cos2": cos2,
                "twin_mean3_adj": float(np.mean(adj[:3])), "_twin_file": self.filenames[i0]}

    def to_state(self):
        """Plain-dict state for the bundle (unpickles without importing this module by name)."""
        return {"filenames": list(self.filenames), "U": self.U, "S": self.S,
                "thresholds": list(self.thresholds), "Y": self.Y}

    @classmethod
    def from_state(cls, st):
        return cls(st["filenames"], st["U"], st["S"][:, 0], st["S"][:, 1], st["thresholds"], st["Y"])


def twin_features(index, means, row: dict, threshold, exclude_filename=None):
    """Twin columns for one (circuit, threshold); falls back to the training means when the
    motif vector is missing (stepper fallback, st_ok < 1) or the lookup fails."""
    try:
        u = motif_vector(row) if index is not None else None
        if u is not None:
            nq = max(float(row.get("n_qubits", 0.0) or 0.0), 1.0)
            ng = max(float(row.get("n_gates", 0.0) or 0.0), 1.0)
            r = index.query(u, math.log10(nq), math.log10(ng), threshold,
                            exclude_filename=exclude_filename)
            if r is not None:
                return {c: r[c] for c in TWIN_COLS}
    except Exception as e:  # never fail inference over the twin lookup
        print("twin_features: lookup failed (%r); using training means" % (e,), file=sys.stderr)
    return {c: float(means.get(c, 0.0)) for c in TWIN_COLS}


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
        if bundle.get("kind") == "blend":  # mean of member log10 predictions (train.py --blend)
            self.members = [(m["cols"], m["model"]) for m in bundle["members"]]
        else:  # legacy single-model bundle {"model", "cols"}
            self.members = [(bundle["cols"], bundle["model"])]
        self._derived_keys = set(derive({}, 16))
        tw = bundle.get("twin")  # structural-twin index (train.py, twin enabled)
        self.twin_index = TwinIndex.from_state(tw["index"]) if tw else None
        self.twin_means = dict(tw["means"]) if tw else {}
        if tw:
            self._derived_keys |= set(TWIN_COLS)
        # raw feature columns the members read (used for an all-zero row if parsing fails)
        self._raw_cols = sorted({c for cols, _ in self.members for c in cols} - self._derived_keys)
        self._warned = set()
        # Smoke test: a missing/broken stepper binary must fail here, not mid-holdout.
        st = step_features(_SMOKE_QASM)
        if st.get("st_ok") != 1:
            raise RuntimeError(
                "qasm_step binary missing or broken (found: %r, st_ok=%r). Build it with "
                "stepper/build.sh (macOS/Linux) or stepper/build.ps1 (Windows), then re-run "
                "install_into_harness.sh so qasm_step[.exe] sits next to model.py."
                % (find_binary(), st.get("st_ok")))

    def featurize(self, qasm_text: str) -> dict:
        # Must never raise: a circuit that crashes featurize scores zero on all its rows.
        t0 = time.perf_counter()
        try:
            h = start_step(qasm_text, t_start=t0)
        except Exception as e:
            print("featurize: start_step failed (%r); using fallback" % (e,), file=sys.stderr)
            h = None
        try:
            # static parser stops at t0 + 9 s and extrapolates its additive counts
            f = _featurize(qasm_text, deadline_at=t0 + 9.0)
        except Exception as e:
            print("featurize: parser failed (%r); using all-zero features" % (e,), file=sys.stderr)
            f = {c: 0.0 for c in self._raw_cols}
        elapsed = time.perf_counter() - t0
        try:
            if h is None:
                raise RuntimeError("no stepper handle")
            st = finish_step(h, timeout_s=max(1.0, 12.0 - elapsed),
                             n2q_hint=f.get("n_2q", 0) + f.get("n_3q_plus", 0))
        except Exception as e:
            print("featurize: stepper failed (%r); trying pure-Python fallback" % (e,), file=sys.stderr)
            try:
                from qasm_step_py import py_step
                st = py_step(qasm_text, deadline_s=max(0.5, 12.0 - (time.perf_counter() - t0)),
                             n2q_hint=f.get("n_2q", 0) + f.get("n_3q_plus", 0))
            except Exception as e2:
                print("featurize: fallback failed too (%r); st_* = 0.0" % (e2,), file=sys.stderr)
                st = {}
        return {**{c: 0.0 for c in self._raw_cols}, **f, **st}

    def predict(self, features: dict, threshold: int) -> float:
        row = {**features, **derive(features, threshold)}
        if self.twin_index is not None:
            row.update(twin_features(self.twin_index, self.twin_means, features, threshold))
        logs = []
        for cols, m in self.members:
            missing = [c for c in cols if c not in row and c not in self._warned]
            if missing:
                self._warned.update(missing)
                print("predict: %d model column(s) missing from features, using 0.0: %s"
                      % (len(missing), ", ".join(missing)), file=sys.stderr)
            x = np.array([[float(row.get(c, 0.0)) for c in cols]])
            logs.append(float(m.predict(x)[0]))
        sec = 10 ** (sum(logs) / len(logs))  # mean in log10 space
        # Timeout rule: if ANY member predicts >= 60% of the cap, call it a timeout.
        # Predicting >= cap gets full credit on a real timeout row, and a success row
        # that any member already puts above 3.6 h loses at most 0.22 dex. On the
        # out-of-fold blend this lifts timeout rows from 0.708 to 0.813 and costs the
        # other rows 0.0003 (the old rule, blend >= 0.8*cap, scored 0.722 on timeouts).
        if max(logs) >= math.log10(TIMEOUT_RULE_FRAC * CAP_SECONDS):
            sec = CAP_SECONDS
        sec = min(sec, CAP_SECONDS)  # the metric doesn't cap predictions on success rows
        return float(sec)
