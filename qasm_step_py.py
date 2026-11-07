"""
Pure-Python fallback for the C++ moment stepper (stepper/qasm_step).

Implements the same state model (plan section B) as a streaming pass with full
custom-gate expansion, under a wall-clock deadline. Used only when the binary is
missing, fails or times out, and for a deterministic ~3 % of training rows.

st_ok: 0.75 = fallback finished, 0.5 = fallback hit its deadline (costs scaled by
n_2q_estimate / n_2q_processed), 0.25 = deadline hit before any 2q gate was processed
(st_log10_cost_T guessed as log10(1 + n2q_hint * 4^min(4, log2 T)), everything else as
for 0.5), 0.0 = fallback itself crashed (all zeros). The effective deadline is >= 1.5 s.

Choices the binary contract leaves open (reconcile with stepper/qasm_step.cpp):
  * st_log10_cost_T = log10(1 + cost_T)  (never -inf)
  * st_n_entangling_2q = number of cut updates (a non-adjacent edge (a,b) is b-a
    updates; a 3q+ gate is a star of edges to its last operand)
  * st_frac_2q_sat_T = saturated cut updates / st_n_entangling_2q
  * st_n_noop_2q_* count gates (2q and 3q+)
  * st_cost_frac_q25/50/75: cost_1024 sampled after every `iv`-th gate (iv starts at 1 and
    doubles, dropping every other sample, whenever 256 samples are held); piecewise-linear
    through (0, 0), the samples and (n_gates, final cost).
  * cswap/fredkin (3 operands): controlled swap; entangles iff the control is non-basis
    (then both targets become non-basis), otherwise swaps the targets' flags; the control
    is never marked non-basis.
  * QFT keys = 0 (not implemented in the fallback).
  * Structure fingerprints (st_fam_*, st_motif_*) = 0: the fallback does not compute them.
  * st_log10_cost_ep_T: each applied 2q edge charged once with the max over its cuts of the
    per-cut charge (computed before that cut's update); st_log10_cut_updates =
    log10(1 + st_n_entangling_2q); st_mean_span_2q = mean (b-a) over applied edges.
Gate classes and rules mirror make_builtins() / Stepper::run in stepper/qasm_step.cpp.
"""
from __future__ import annotations

import math
import re
import time

from qasm_features import _Scope, _split_call

DEFAULT_THRESHOLDS = (16, 64, 256, 512, 1024)
TWO_PI = 2.0 * math.pi
EPS = 1e-9
MAX_PRIMS = 200_000_000
MAX_DEPTH = 64


def st_cols(thresholds=DEFAULT_THRESHOLDS):
    cols = ["st_ok", "st_n_qubits", "st_n_gates", "st_n_2q", "st_n_3q_plus", "st_n_measure",
            "st_n_reset", "st_n_entangling_2q", "st_n_noop_2q_classical",
            "st_n_noop_2q_identity", "st_chi_max", "st_chi_mean_final",
            "st_frac_qubits_basis_final", "st_n_if", "st_truncated", "st_n_unknown_gates",
            "st_n_skipped_stmts", "st_n_param_parse_err"]
    cols += [f"st_log10_cost_{t}" for t in thresholds]
    cols += [f"st_frac_2q_sat_{t}" for t in thresholds]
    cols += ["st_cost_frac_q25", "st_cost_frac_q50", "st_cost_frac_q75",
             "st_n_qft_blocks", "st_qft_max_size", "st_qft_frac_2q"]
    cols += [f"st_log10_cost_ep_{t}" for t in thresholds]
    cols += ["st_log10_cut_updates", "st_mean_span_2q"]
    cols += FP_COLS
    return cols


# structure fingerprints: computed only by the binary; the fallback emits 0 for all of them
FP_COLS = ["st_fam_adder_blocks", "st_fam_adder_frac_2q", "st_fam_sycamore",
           "st_fam_sycamore_pairs", "st_fam_sycamore_top4_frac", "st_fam_sycamore_period",
           "st_fam_ghz_chain", "st_fam_ghz_chain_frac", "st_fam_grover_iters",
           "st_fam_grover_frac", "st_fam_layer_period", "st_fam_layer_period_frac",
           "st_fam_bv_oracle", "st_fam_bv_fanin_frac", "st_fam_mcphase_arith",
           "st_fam_mcphase_arith_frac"]
FP_COLS += [f"st_motif_{i}" for i in range(64)] + ["st_motif_entropy", "st_motif_top1_frac"]


# --- gate classes: mirror make_builtins() in stepper/qasm_step.cpp ----------
KEEP1, MIX1, ROT1, DIAG, CPERM, CMIX, SWAPLIKE, ALWAYS = range(8)
CLS: dict = {}
ANGLE = set()  # single-angle gates: identity iff params[0] = 0 mod 2pi


def _add(names, cls, angle=False):
    for n in names.split():
        CLS[n] = cls
        if angle:
            ANGLE.add(n)


_add("id i x y z s sdg t tdg p phase rz u1 u0 X Y Z gphase delay", KEEP1)
_add("h sx sxdg u2 sy sydg H", MIX1)
_add("rx ry u u3 U r", ROT1)
_add("cz ccz cs csdg ct ctdg mcz c3z CZ", DIAG)
_add("cp cphase crz cu1 rzz mcphase mcp mcu1 zz", DIAG, True)
_add("cx CX cnot cy ccx toffoli mcx c3x c4x rccx rcccx rc3x mcx_gray mcx_vchain "
     "mcx_recursive ccy", CPERM)
_add("ch cu cu3 cu2 csx c3sx c3sqrtx mcu mcrx mcry", CMIX)
_add("crx cry rzx", CMIX, True)
_add("swap iswap dcx cswap fredkin", SWAPLIKE)
_add("ecr xx_plus_yy xx_minus_yy fsim", ALWAYS)
_add("rxx ryy", ALWAYS, True)
SKIP_KW = ("OPENQASM", "include", "barrier", "opaque", "input", "output", "const", "int",
           "uint", "float", "angle", "bool", "duration", "stretch", "for", "while", "def",
           "box", "pragma", "cal", "defcal", "let", "extern", "return", "break",
           "continue", "end", "array", "complex")

# --- parameter expressions ----------------------------------------------------
_TOK = re.compile(r"\s*(?:(\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)|([A-Za-z_]\w*|π)|([-+*/^(),]))")
_FUNCS = {"sin": math.sin, "cos": math.cos, "tan": math.tan, "exp": math.exp, "ln": math.log,
          "sqrt": math.sqrt, "asin": math.asin, "acos": math.acos, "atan": math.atan,
          "arcsin": math.asin, "arccos": math.acos, "arctan": math.atan}
_CONSTS = {"pi": math.pi, "π": math.pi, "tau": TWO_PI, "euler": math.e}
_BASE_NS = {"__builtins__": {}}
_BASE_NS.update({"_v_" + k: v for k, v in _FUNCS.items()})
_BASE_NS.update({"_v_pi": math.pi, "_v_tau": TWO_PI, "_v_euler": math.e})
_expr_cache: dict = {}


def _compile_expr(expr):
    """Whitelisted tokens -> python code object (identifiers renamed _v_*), or None."""
    hit = _expr_cache.get(expr, 0)
    if hit != 0:
        return hit
    out, pos, s = [], 0, expr.strip()
    code = None
    ok = bool(s)
    while ok and pos < len(s):
        m = _TOK.match(s, pos)
        if not m or m.end() == pos:
            ok = False
            break
        pos = m.end()
        num, ident, op = m.groups()
        if num is not None:
            out.append(repr(float(num)))
        elif ident is not None:
            out.append("_v_" + ("pi" if ident == "π" else ident))
        else:
            out.append("**" if op == "^" else op)
    if ok:
        try:
            code = compile(" ".join(out), "<qasm-param>", "eval")
        except (SyntaxError, ValueError):
            code = None
    if len(_expr_cache) < 500_000:
        _expr_cache[expr] = code
    return code


def _split_params(s):
    """Top-level comma split of a parameter list string."""
    parts, depth, cur = [], 0, 0
    for i, c in enumerate(s):
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "," and depth == 0:
            parts.append(s[cur:i])
            cur = i + 1
    parts.append(s[cur:])
    return [p.strip() for p in parts if p.strip()]


def _matching(s, i):
    d = 0
    for j in range(i, len(s)):
        if s[j] == "(":
            d += 1
        elif s[j] == ")":
            d -= 1
            if d == 0:
                return j
    return -1


class _Deadline(Exception):
    pass


class _Stepper:
    def __init__(self, thresholds, deadline_at):
        self.T = tuple(thresholds)
        self.L = tuple(int(round(math.log2(t))) for t in self.T)
        self.nT = len(self.T)
        self.deadline_at = deadline_at
        self.sc = _Scope({})
        self.defs = {}           # name -> (param_names, n_args, [(gname, exprs, local_qubits)])
        self.n = 0
        self.chi = []
        self.basis = []
        self.entq = []           # qubit took part in an entangling update since last measure/reset
        # basis fraction ignores trailing measure/reset runs (snapshot before them), as the binary
        self.gate_since_snap = True
        self.basis_snap = 0
        self.cost = [0.0] * self.nT
        self.cost_ep = [0.0] * self.nT  # each 2q edge charged once, at its most expensive cut
        self.epmax = [0.0] * self.nT
        self.span_sum = 0
        self.n_edges = 0
        self.sat = [0] * self.nT
        self.n_updates = 0
        self.c = dict(n_gates=0, n_2q=0, n_3q=0, meas=0, reset=0, ent=0, noop_cls=0,
                      noop_id=0, n_if=0, unknown=0, skipped=0, perr=0)
        self.snaps = []          # cost at the 1024 (last) threshold after gate snap_idx[i]
        self.snap_idx = []
        self.snap_iv = 1         # doubles (dropping every other sample) at 256 samples

    # -- qubit space -----------------------------------------------------
    def _sync(self):
        n = self.sc.n_q
        if n != self.n:
            self.basis.extend([True] * (n - self.n))
            self.entq.extend([False] * (n - self.n))
            self.chi.extend([0] * (max(n - 1, 0) - len(self.chi)))
            self.n = n

    # -- params ----------------------------------------------------------
    def _eval(self, expr, env):
        code = _compile_expr(expr)
        if code is None:
            self.c["perr"] += 1
            return None
        ns = _BASE_NS if not env else {**_BASE_NS, **env}
        try:
            v = float(eval(code, ns))  # noqa: S307 - whitelisted tokens only
        except Exception:
            self.c["perr"] += 1
            return None
        if not math.isfinite(v):
            self.c["perr"] += 1
            return None
        return v

    # -- entanglement state ----------------------------------------------
    def _upd(self, k, r):
        chi = self.chi
        c = chi[k]
        left = chi[k - 1] if k > 0 else 0
        right = chi[k + 1] if k + 1 < len(chi) else 0
        cost, sat, epmax = self.cost, self.sat, self.epmax
        for i, L in enumerate(self.L):
            if c >= L:
                sat[i] += 1
            d = 2.0 ** ((left if left < L else L) + (right if right < L else L))
            cost[i] += d
            if d > epmax[i]:
                epmax[i] = d
        self.n_updates += 1
        self.c["ent"] += 1
        new = min(c + r, left + 1, right + 1, k + 1, self.n - k - 1)
        if new > c:
            chi[k] = new

    # -- primitive gate (same rules as Stepper::run in qasm_step.cpp) -----
    def _prim(self, name, vals, qs):
        c = self.c
        g = c["n_gates"]
        if g and g % self.snap_iv == 0:  # sample after gate g was applied (as the binary does)
            self._sample(g)
        g += 1
        c["n_gates"] = g
        self.gate_since_snap = True
        if g & 4095 == 0:
            if time.perf_counter() > self.deadline_at or g >= MAX_PRIMS:
                raise _Deadline
        basis = self.basis
        k = len(qs)
        cls = CLS.get(name)
        if cls is None:
            c["unknown"] += 1
            cls = ALWAYS
        if k == 1:
            q = qs[0]
            if cls == KEEP1 or cls == DIAG or cls == CPERM:
                return
            if cls == ROT1:
                t = vals[0] if vals else None
                if t is not None:
                    t = abs(t) % math.pi
                    if t < EPS or math.pi - t < EPS:
                        return
            basis[q] = False
            return
        if k == 2:
            c["n_2q"] += 1
        else:
            c["n_3q"] += 1
        if name in ANGLE and vals and vals[0] is not None:
            t = abs(vals[0]) % TWO_PI
            if t < EPS or TWO_PI - t < EPS:
                c["noop_id"] += 1
                return
        tq = qs[-1]
        if cls == DIAG:
            ent = sum(1 for q in qs if not basis[q]) >= 2
        elif cls == CPERM:
            ent = any(not basis[q] for q in qs[:-1])
            if ent:
                basis[tq] = False
        elif cls == CMIX:
            ent = any(not basis[q] for q in qs[:-1])
            basis[tq] = False
        elif cls == SWAPLIKE and name in ("cswap", "fredkin") and k == 3:
            # controlled swap: classical while the control is a basis state
            _, a, b = qs
            ent = not basis[qs[0]]
            if ent:
                basis[a] = basis[b] = False
            else:
                entq = self.entq
                basis[a], basis[b] = basis[b], basis[a]
                entq[a], entq[b] = entq[b], entq[a]
        elif cls == SWAPLIKE and name == "swap" and k == 2:
            # plain swap: product states stay product; exchange the qubit flags
            a, b = qs
            entq = self.entq
            ent = entq[a] or entq[b]
            basis[a], basis[b] = basis[b], basis[a]
            entq[a], entq[b] = entq[b], entq[a]
        elif cls == SWAPLIKE:
            ent = any(not basis[q] for q in qs)
            if ent:
                for q in qs:
                    basis[q] = False
        else:  # ALWAYS, unknown, or a 1q class used with >= 2 operands
            ent = True
            for q in qs:
                basis[q] = False
        if not ent:
            c["noop_cls"] += 1
            return
        for q in qs:
            self.entq[q] = True
        for o in qs[:-1]:
            if o == tq:
                continue
            lo, hi = (o, tq) if o < tq else (tq, o)
            r = 2 if (name == "swap" and k == 2 and hi - lo == 1) else 1
            epmax = self.epmax
            for i in range(self.nT):
                epmax[i] = 0.0
            for kk in range(lo, hi):
                self._upd(kk, r)
            for i in range(self.nT):
                self.cost_ep[i] += epmax[i]
            self.span_sum += hi - lo
            self.n_edges += 1

    # -- gate call (custom expansion) -----------------------------------
    def _call(self, name, vals, qs, depth):
        d = self.defs.get(name)
        if d is not None and d[1] == len(qs) and depth < MAX_DEPTH:
            pnames, _, body = d
            env = {"_v_" + p: (v if v is not None else 0.1) for p, v in zip(pnames, vals)}
            for gname, exprs, lq in body:
                bv = [self._eval(e, env) for e in exprs]
                self._call(gname, bv, tuple(qs[i] for i in lq), depth + 1)
            return
        self._prim(name, vals, qs)

    # -- statements ------------------------------------------------------
    def _parse_call(self, s):
        name, ops, has_param = _split_call(s)
        exprs = []
        if has_param:
            p = s.find("(")
            q = _matching(s, p)
            if q < 0:
                self.c["perr"] += 1
            else:
                exprs = _split_params(s[p + 1:q])
                ops = s[q + 1:]
        return name.strip(), exprs, ops

    def stmt(self, s):
        c0 = s[0]
        if c0 == "i" and s.startswith("if") and (len(s) == 2 or s[2] in " ("):
            self.c["n_if"] += 1
            p = s.find("(")
            q = _matching(s, p) if p >= 0 else -1
            body = s[q + 1:].strip() if q >= 0 else ""
            if body:
                self.stmt(body)
            return
        if c0 == "m" and s.startswith("measure"):
            rest = s[7:]
            arrow = rest.find("->")
            self._measure(rest[:arrow] if arrow >= 0 else rest)
            return
        if "=" in s and "measure" in s:     # v3: c[0] = measure q[0]
            eq = s.find("=")
            mi = s.find("measure", eq + 1)
            if mi >= 0:
                self._measure(s[mi + 7:])
                return
        if (c0 == "q" and (s.startswith("qreg") or s.startswith("qubit"))) or \
                (c0 == "c" and s.startswith("creg")) or (c0 == "b" and s.startswith("bit")):
            try:
                self.sc.declare(s)
            except Exception:
                self.c["skipped"] += 1
            self._sync()
            return
        if c0 == "r" and s.startswith("reset"):
            res = self.sc._resolve(s[5:])
            self.c["reset"] += max(1, len(res))
            for tup in res:
                for q in tup:
                    self._snap()
                    self.basis[q] = True
                    self.entq[q] = False
            return
        if s.startswith(SKIP_KW):
            w = s.split(None, 1)[0].split("(")[0]
            if w not in ("OPENQASM", "include", "barrier"):
                self.c["skipped"] += 1
            return
        name, exprs, ops = self._parse_call(s)
        vals = [self._eval(e, None) for e in exprs]
        for qs in self.sc._resolve(ops):
            if qs:
                self._call(name, vals, qs, 0)

    def _sample(self, g):
        self.snaps.append(self.cost[-1])
        self.snap_idx.append(g)
        if len(self.snaps) >= 256:
            iv2 = 2 * self.snap_iv
            keep = [i for i, x in enumerate(self.snap_idx) if x % iv2 == 0]
            self.snaps = [self.snaps[i] for i in keep]
            self.snap_idx = [self.snap_idx[i] for i in keep]
            self.snap_iv = iv2

    def _snap(self):
        if self.gate_since_snap:
            self.basis_snap = sum(self.basis)
            self.gate_since_snap = False

    def _measure(self, opstr):
        res = self.sc._resolve(opstr)
        n = 0
        for tup in res:
            for q in tup:
                self._snap()
                self.basis[q] = True
                self.entq[q] = False
                n += 1
        self.c["meas"] += n or 1

    def gate_def(self, header, body_stmts):
        h = header[4:].strip()
        p = h.find("(")
        sp = h.find(" ")
        if p >= 0 and (sp < 0 or p < sp):
            name = h[:p].strip()
            q = _matching(h, p)
            pnames = [x.strip() for x in h[p + 1:q].split(",") if x.strip()]
            args = h[q + 1:]
        else:
            parts = h.split(None, 1)
            name, pnames = parts[0], []
            args = parts[1] if len(parts) > 1 else ""
        arg_names = [a.strip() for a in args.split(",") if a.strip()]
        local = _Scope({}, local_args=arg_names)
        body = []
        for s in body_stmts:
            if s.startswith("barrier") or s.startswith("//"):
                continue
            gname, exprs, ops = self._parse_call(s)
            for lq in local._resolve(ops):
                if lq:
                    body.append((gname, exprs, lq))
        self.defs[name] = (pnames, len(arg_names), body)


_LEX = re.compile(r"([^;{}]*)([;{}])")
_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)


def _result(st, thresholds, ok, scale):
    c = st.c
    out = dict.fromkeys(st_cols(thresholds), 0.0)
    chi = st.chi
    out.update({
        "st_ok": ok, "st_n_qubits": float(st.n), "st_n_gates": float(c["n_gates"]),
        "st_n_2q": float(c["n_2q"]), "st_n_3q_plus": float(c["n_3q"]),
        "st_n_measure": float(c["meas"]), "st_n_reset": float(c["reset"]),
        "st_n_entangling_2q": float(c["ent"]), "st_n_noop_2q_classical": float(c["noop_cls"]),
        "st_n_noop_2q_identity": float(c["noop_id"]),
        "st_chi_max": float(max(chi)) if chi else 0.0,
        "st_chi_mean_final": float(sum(chi) / len(chi)) if chi else 0.0,
        "st_frac_qubits_basis_final": ((sum(st.basis) if st.gate_since_snap else st.basis_snap)
                                       / st.n) if st.n else 0.0,
        "st_n_if": float(c["n_if"]), "st_truncated": 1.0 if c["n_gates"] >= MAX_PRIMS else 0.0,
        "st_n_unknown_gates": float(c["unknown"]), "st_n_skipped_stmts": float(c["skipped"]),
        "st_n_param_parse_err": float(c["perr"]),
    })
    for i, t in enumerate(st.T):
        out[f"st_log10_cost_{t}"] = math.log10(1.0 + st.cost[i] * scale)
        out[f"st_frac_2q_sat_{t}"] = st.sat[i] / st.n_updates if st.n_updates else 0.0
        out[f"st_log10_cost_ep_{t}"] = math.log10(1.0 + st.cost_ep[i] * scale)
    out["st_log10_cut_updates"] = math.log10(1.0 + c["ent"])
    out["st_mean_span_2q"] = st.span_sum / st.n_edges if st.n_edges else 0.0
    total = st.cost[-1]
    g = c["n_gates"]
    if g and g % st.snap_iv == 0 and (not st.snap_idx or st.snap_idx[-1] != g):
        st._sample(g)
    for q, key in ((0.25, "st_cost_frac_q25"), (0.5, "st_cost_frac_q50"), (0.75, "st_cost_frac_q75")):
        out[key] = _cost_fraction(st.snaps, st.snap_idx, g, total, q)
    return {k: float(v) for k, v in out.items()}


def _cost_fraction(samples, sample_idx, n_gates, final_cost, q):
    """Same piecewise-linear interpolation as cost_fraction() in qasm_step.cpp."""
    if final_cost <= 0 or n_gates == 0:
        return 0.0
    x = q * n_gates
    px = py = 0.0
    for i in range(len(samples) + 1):
        if i < len(samples):
            nx, ny = float(sample_idx[i]), samples[i]
        else:
            nx, ny = float(n_gates), final_cost
        if x <= nx:
            f = (x - px) / (nx - px) if nx > px else 1.0
            return (py + f * (ny - py)) / final_cost
        px, py = nx, ny
    return 1.0


def py_step(text: str, deadline_s: float = 12.0, n2q_hint=None,
            thresholds=DEFAULT_THRESHOLDS) -> dict:
    """Fallback stepper. Never raises; every st_* key is a finite float."""
    thresholds = tuple(thresholds or DEFAULT_THRESHOLDS)
    t0 = time.perf_counter()
    try:
        st = _Stepper(thresholds, t0 + max(1.5, deadline_s))
    except Exception:
        return {**dict.fromkeys(st_cols(thresholds), 0.0)}
    ok, scale = 0.75, 1.0
    guess = False
    try:
        if "//" in text or "/*" in text:
            text = _COMMENT.sub(" ", text)
        stack = []           # block kinds: 'gate', 'if', 'skip'
        gate_hdr, gate_body = None, None
        nstmt = 0
        for m in _LEX.finditer(text):
            s = m.group(1).strip()
            d = m.group(2)
            nstmt += 1
            if nstmt & 4095 == 0 and time.perf_counter() > st.deadline_at:
                raise _Deadline
            if d == ";":
                if not s:
                    continue
                if gate_body is not None:
                    gate_body.append(s)
                elif "skip" not in stack:
                    st.stmt(s)
            elif d == "{":
                if s.startswith("gate") and gate_body is None:
                    gate_hdr, gate_body = s, []
                    stack.append("gate")
                elif s.startswith("if") or s.startswith("else"):
                    if s.startswith("if"):
                        st.c["n_if"] += 1
                    stack.append("if")
                else:
                    if "skip" not in stack:
                        st.c["skipped"] += 1
                    stack.append("skip")
            else:  # '}'
                if s:
                    if gate_body is not None:
                        gate_body.append(s)
                    elif "skip" not in stack:
                        st.stmt(s)
                kind = stack.pop() if stack else None
                if kind == "gate" and gate_body is not None:
                    try:
                        st.gate_def(gate_hdr, gate_body)
                    except Exception:
                        st.c["skipped"] += 1
                    gate_hdr, gate_body = None, None
    except _Deadline:
        ok = 0.5
        done = st.c["n_2q"] + st.c["n_3q"]
        if n2q_hint and done > 0 and n2q_hint > done:
            scale = float(n2q_hint) / done
        if done == 0:
            ok, guess = 0.25, True
    except Exception:
        ok = 0.5
    try:
        out = _result(st, thresholds, ok, scale)
        if guess:  # nothing processed: saturated-adjacent-gate guess from the 2q hint
            h = float(n2q_hint or 0)
            for t in thresholds:
                out[f"st_log10_cost_{t}"] = math.log10(1.0 + h * 4.0 ** min(4.0, math.log2(t)))
        return out
    except Exception:
        return {**dict.fromkeys(st_cols(thresholds), 0.0)}


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    for p in sys.argv[1:]:
        t = time.perf_counter()
        r = py_step(Path(p).read_text(encoding="utf-8", errors="replace"))
        print(f"{Path(p).name} {time.perf_counter() - t:.2f}s", json.dumps(r))
