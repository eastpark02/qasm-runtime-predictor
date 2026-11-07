"""
Version-aware streaming OpenQASM 2.0 / 3.0 feature extractor.

One pass over the text, never builds a circuit object. Handles:
  * headers `OPENQASM 2.0;` / `OPENQASM 3.0;` (version is emitted as a feature)
  * v2 `qreg name[n]; creg name[n];`   v3 `qubit[n] name; qubit name; bit[n] name;`
  * multiple registers -> one global qubit index space
  * gate calls with params, register broadcast (`h q;`)
  * v2 `measure q[i] -> c[i];`  v3 `c[i] = measure q[i];`
  * `reset`, `barrier`, v2 inline `if (c==1) x q[0];`, v3 `if (...) { ... } else { ... }`
  * custom `gate` definitions (single- or multi-line, nested calls): each body is
    summarised once (counts, depth, 2q interaction pairs) and that summary is
    applied at every call site. No inline expansion, so 500k definitions stay cheap.

Public API:  featurize(qasm_text: str, deadline_at=None) -> dict[str, float]

deadline_at (a time.perf_counter() value): the clock is read every ~1024 lines, every
1024 statements of a gate body and every 1024 statements of a multi-statement line. If it
passes, parsing stops and the additive features are extrapolated by 1/f (f = fraction of
lines consumed); output for a file that finishes in time is unchanged.
"""

from __future__ import annotations

import math
import re
import time
from collections import Counter

_PARAMS_RE = re.compile(r"\([^()]*(?:\([^()]*\)[^()]*)*\)")

# Gates that are not Clifford for generic angles (good proxy for "hard" work).
NON_CLIFFORD = frozenset("""
t tdg rx ry rz p phase u u1 u2 u3 cp cphase crx cry crz cu cu1 cu3 rxx ryy rzz rzx
ccx ccz rccx rcccx c3x c3sx c4x cswap mcx mcphase xx_minus_yy xx_plus_yy fsim
""".split())
SKIP_PREFIXES = ("include", "OPENQASM", "//", "opaque", "input", "output", "const",
                 "int", "uint", "float", "angle", "bool", "duration", "stretch")


class _ParseDeadline(Exception):
    """Raised inside a gate body / long line when the deadline passes; arg = fraction done."""


_SLICE = 1024


class _Summary:
    """Aggregated effect of one custom gate body, in local qubit indices."""
    __slots__ = ("n_args", "counts", "depth", "depth2q", "pairs", "names")

    def __init__(self, n_args):
        self.n_args = n_args
        self.counts = [0] * _NC      # see _C_* indices
        self.depth = 0
        self.depth2q = 0
        self.pairs: Counter = Counter()  # (a, b) local, a<b  -> multiplicity
        self.names: Counter = Counter()  # primitive gate name -> count


# counter indices
_C_TOTAL, _C_1Q, _C_2Q, _C_3Q, _C_NONCLIFF, _C_MEAS, _C_RESET, _C_COND, _C_PARAM = range(9)
_NC = 9


class _Scope:
    """State for scanning either the top-level program or one gate body."""

    def __init__(self, gates, local_args=None):
        self.gates = gates                  # name -> _Summary (shared)
        self.regs = {}                      # name -> (offset, size)
        self.n_q = 0
        self.n_c = 0
        self.cregs = set()
        self.level = []                     # per-qubit depth
        self.level2 = []                    # per-qubit 2q-only depth
        self.counts = [0] * _NC
        self.names = Counter()
        self.pairs = Counter()
        self.cache = {}                     # operand string -> tuple of qubit idx
        self.n_barrier = 0
        self.n_custom_calls = 0
        if local_args is not None:
            for a in local_args:
                self.regs[a] = (self.n_q, 1)
                self._grow(1)
            self.local = True
        else:
            self.local = False

    def _grow(self, k):
        self.n_q += k
        self.level.extend([0] * k)
        self.level2.extend([0] * k)

    # -- operand resolution ------------------------------------------------
    def _resolve(self, opstr):
        """'a[1], b[2]' -> list of qubit tuples (one tuple per broadcast step)."""
        hit = self.cache.get(opstr)
        if hit is not None:
            return hit
        groups = []
        width = 1
        for tok in opstr.split(","):
            tok = tok.strip()
            if not tok:
                continue
            br = tok.find("[")
            if br >= 0:
                name = tok[:br].strip()
                reg = self.regs.get(name)
                if reg is None:
                    groups.append(None)
                    continue
                inner = tok[br + 1:tok.rfind("]")]
                if ":" in inner:  # v3 slice a[0:3]
                    lo, hi = (int(x) for x in inner.split(":")[:2])
                    idxs = [reg[0] + i for i in range(lo, hi + 1)]
                    groups.append(idxs)
                    width = max(width, len(idxs))
                else:
                    try:
                        groups.append([reg[0] + int(inner)])
                    except ValueError:
                        groups.append(None)
            else:
                reg = self.regs.get(tok)
                if reg is None:
                    groups.append(None)
                    continue
                idxs = list(range(reg[0], reg[0] + reg[1]))
                groups.append(idxs)
                width = max(width, len(idxs))
        groups = [g for g in groups if g is not None]
        if width == 1:
            res = (tuple(g[0] for g in groups),) if groups else ()
        else:  # register broadcast
            res = tuple(tuple(g[i] if len(g) > 1 else g[0] for g in groups) for i in range(width))
        if len(self.cache) < 200_000:
            self.cache[opstr] = res
        return res

    # -- declarations ------------------------------------------------------
    def declare(self, stmt):
        s = stmt
        if s.startswith("qreg"):
            name, size = s[4:].strip().rstrip("]").split("[")
            self.regs[name.strip()] = (self.n_q, int(size))
            self._grow(int(size))
        elif s.startswith("creg"):
            name, size = s[4:].strip().rstrip("]").split("[")
            self.cregs.add(name.strip())
            self.n_c += int(size)
        elif s.startswith("qubit"):
            rest = s[5:].strip()
            if rest.startswith("["):
                size, name = rest[1:].split("]")
                size = int(size)
            else:
                size, name = 1, rest
            self.regs[name.strip()] = (self.n_q, size)
            self._grow(size)
        elif s.startswith("bit"):
            rest = s[3:].strip()
            if rest.startswith("["):
                size, name = rest[1:].split("]")
                self.cregs.add(name.split("=")[0].strip())
                self.n_c += int(size)
            else:
                self.cregs.add(rest.split("=")[0].strip())
                self.n_c += 1

    # -- applying operations -----------------------------------------------
    def apply(self, name, opstr, has_param, cond):
        counts = self.counts
        lv = self.level
        lv2 = self.level2
        g = self.gates.get(name)
        for qs in self._resolve(opstr):
            k = len(qs)
            if k == 0:
                continue
            if g is not None and g.n_args == k:
                self._apply_custom(g, qs, cond)
                continue
            counts[_C_TOTAL] += 1
            self.names[name] += 1
            if has_param:
                counts[_C_PARAM] += 1
            if cond:
                counts[_C_COND] += 1
            if name in NON_CLIFFORD:
                counts[_C_NONCLIFF] += 1
            if k == 1:
                counts[_C_1Q] += 1
                q = qs[0]
                lv[q] += 1
            elif k == 2:
                counts[_C_2Q] += 1
                a, b = qs
                d = (lv[a] if lv[a] > lv[b] else lv[b]) + 1
                lv[a] = lv[b] = d
                d2 = (lv2[a] if lv2[a] > lv2[b] else lv2[b]) + 1
                lv2[a] = lv2[b] = d2
                self.pairs[(a, b) if a < b else (b, a)] += 1
            else:
                counts[_C_3Q] += 1
                d = max(lv[q] for q in qs) + 1
                d2 = max(lv2[q] for q in qs) + 1
                for q in qs:
                    lv[q] = d
                    lv2[q] = d2
                # decompose multi-qubit gate into a chain of pairs for interaction stats
                sq = sorted(qs)
                for i in range(len(sq) - 1):
                    self.pairs[(sq[i], sq[i + 1])] += 1

    def _apply_custom(self, g: _Summary, qs, cond):
        self.n_custom_calls += 1
        c = self.counts
        gc = g.counts
        for i in range(_NC):
            c[i] += gc[i]
        if cond:
            c[_C_COND] += gc[_C_TOTAL]
        self.names.update(g.names)
        lv, lv2 = self.level, self.level2
        d = max(lv[q] for q in qs) + g.depth
        d2 = max(lv2[q] for q in qs) + g.depth2q
        for q in qs:
            lv[q] = d
            lv2[q] = d2
        pairs = self.pairs
        for (a, b), m in g.pairs.items():
            x, y = qs[a], qs[b]
            pairs[(x, y) if x < y else (y, x)] += m

    def measure(self, opstr):
        n = sum(len(t) for t in self._resolve(opstr)) or 1
        self.counts[_C_MEAS] += n

    def reset(self, opstr):
        self.counts[_C_RESET] += max(1, len(self._resolve(opstr)))


def _split_call(stmt):
    """'rz(0.1) q[0], q[1]' -> ('rz', 'q[0], q[1]', True)"""
    p = stmt.find("(")
    sp = stmt.find(" ")
    if p >= 0 and (sp < 0 or p < sp):
        close = stmt.rfind(")")
        return stmt[:p].strip(), stmt[close + 1:], True
    if sp < 0:
        return stmt, "", False
    return stmt[:sp], stmt[sp + 1:], False


def _run_statement(sc: _Scope, s: str, cond: bool, state):
    """Process one statement (no trailing ';')."""
    c0 = s[0]
    # fast path: plain gate call starting with a lowercase identifier that isn't a keyword
    if c0 == "m" and s.startswith("measure"):
        rest = s[7:]
        arrow = rest.find("->")
        sc.measure(rest[:arrow] if arrow >= 0 else rest)
        return
    if "=" in s and "measure" in s:            # v3: c[0] = measure q[0]
        sc.measure(s[s.find("measure") + 7:])
        return
    if c0 == "q" and (s.startswith("qreg") or s.startswith("qubit")):
        sc.declare(s)
        return
    if (c0 == "c" and s.startswith("creg ")) or (c0 == "b" and s.startswith("bit")):
        if s.startswith("barrier"):
            sc.n_barrier += 1
            return
        sc.declare(s)
        return
    if c0 == "b" and s.startswith("barrier"):
        sc.n_barrier += 1
        return
    if c0 == "r" and s.startswith("reset"):
        sc.reset(s[5:])
        return
    if c0 == "i" and s.startswith("if"):
        # v2 inline: if(c==1) x q[0]
        close = s.find(")")
        body = s[close + 1:].strip()
        state["n_if"] += 1
        if body:
            _run_statement(sc, body, True, state)
        return
    if s.startswith(SKIP_PREFIXES):
        return
    name, ops, has_param = _split_call(s)
    sc.apply(name, ops, has_param, cond)


def _parse_gate_def(text, gates, state):
    """text = full 'gate name(params) a,b { body }' -> registers a _Summary."""
    lb = text.find("{")
    header = text[4:lb].strip()
    body = text[lb + 1:text.rfind("}")]
    p = header.find("(")
    if p >= 0:
        name = header[:p].strip()
        args = header[header.rfind(")") + 1:]
    else:
        parts = header.split(None, 1)
        name = parts[0]
        args = parts[1] if len(parts) > 1 else ""
    arg_names = [a.strip() for a in args.split(",") if a.strip()]
    state["n_gate_defs"] += 1
    # Many generated files define thousands of gates whose bodies differ only in
    # angle literals (mcphase_0, mcphase_1, ...). Bodies with the same shape have
    # the same summary, so key the cache on the body with parenthesised params removed.
    # Parameters never change which gates/qubits a body touches.
    shape_key = (len(arg_names), _PARAMS_RE.sub("", body), args)
    hit = _shape_cache_get(state, shape_key, gates)
    if hit is not None:
        gates[name] = hit
        return
    sc = _Scope(gates, local_args=arg_names)
    stmts = body.split(";")
    dl = state.get("deadline_at")
    if dl is None or len(stmts) <= _SLICE:
        for st in stmts:
            st = st.strip()
            if st:
                _run_statement(sc, st, False, state)
    else:
        ns = len(stmts)
        for j in range(0, ns, _SLICE):
            if time.perf_counter() > dl:
                raise _ParseDeadline(j / ns)
            for st in stmts[j:j + _SLICE]:
                st = st.strip()
                if st:
                    _run_statement(sc, st, False, state)
    summ = _Summary(len(arg_names))
    summ.counts = sc.counts
    summ.depth = max(sc.level) if sc.level else 0
    summ.depth2q = max(sc.level2) if sc.level2 else 0
    summ.pairs = sc.pairs
    summ.names = sc.names
    gates[name] = summ
    state["shape_cache"][shape_key] = summ


def _shape_cache_get(state, key, gates):
    # Calls inside the body resolve to whatever `gates` currently holds, so a cached
    # summary is only valid if called sub-gates are themselves shape-identical, which
    # holds because the key includes the called gate names verbatim.
    return state["shape_cache"].get(key)


def featurize(text: str, deadline_at=None) -> dict:
    state = {"n_if": 0, "n_gate_defs": 0, "shape_cache": {}, "deadline_at": deadline_at}
    gates: dict = {}
    top = _Scope(gates)
    version = 2.0
    head = text[:200]
    i = head.find("OPENQASM")
    if i >= 0:
        try:
            version = float(head[i + 8:head.find(";", i)].strip())
        except ValueError:
            pass

    lines = text.split("\n")
    n_lines = len(lines)
    pending_gate = None
    cond_depth = 0  # depth of open v3 `if {` blocks
    run = _run_statement
    # Without a deadline the whole file is one slice. With one, the clock is read between
    # slices of `chunk` lines; the slice shrinks when one slice takes > 50 ms (expensive
    # lines, e.g. calls of a wide custom gate), so the overshoot stays small.
    pos = 0
    # With a deadline, start at 16 lines and grow x4 (up to _SLICE) while a slice takes
    # < 5 ms, so a file of a few very expensive lines is still checked early.
    chunk = n_lines if deadline_at is None else 16
    frac = None
    line = None
    pending_first = None  # first line object of an open multi-line gate definition
    try:
        while pos < n_lines:
            if deadline_at is not None:
                t_now = time.perf_counter()
                # reserve time for _finalize's per-pair loop (~0.35 us/pair measured; at
                # most 4 s, which only matters past ~10M distinct qubit pairs)
                if t_now > deadline_at - min(4.0, 4e-7 * len(top.pairs)):
                    frac = pos / n_lines
                    break
            end = pos + chunk
            for line in (lines if deadline_at is None else lines[pos:end]):
                s = line.strip()
                if not s:
                    continue
                if pending_gate is not None:
                    pending_gate.append(s)
                    if "}" in s:
                        _parse_gate_def(" ".join(pending_gate), gates, state)
                        pending_gate = None
                    continue
                c0 = s[0]
                if c0 == "/":
                    continue
                if c0 == "g" and s.startswith("gate ") :
                    if "}" in s:
                        _parse_gate_def(s, gates, state)
                    else:
                        pending_gate = [s]
                        pending_first = line
                    continue
                if c0 == "}":
                    # closes an if/else block (or `} else {`)
                    if "{" in s:
                        continue
                    cond_depth = max(0, cond_depth - 1)
                    continue
                if s[-1] == "{":
                    # v3 `if (cond) {` / `else {`
                    if s.startswith("if") or s.startswith("else"):
                        state["n_if"] += 1
                        cond_depth += 1
                    continue
                if s.count(";") > 1:
                    stmts = s.split(";")
                    if deadline_at is not None and len(stmts) > _SLICE:
                        ns = len(stmts)
                        for j in range(0, ns, _SLICE):
                            if time.perf_counter() > deadline_at:
                                raise _ParseDeadline(j / ns)
                            for st in stmts[j:j + _SLICE]:
                                st = st.strip()
                                if st:
                                    run(top, st, cond_depth > 0, state)
                        continue
                else:
                    stmts = (s[:-1] if s[-1] == ";" else s,)
                for st in stmts:
                    st = st.strip()
                    if st:
                        run(top, st, cond_depth > 0, state)
            if deadline_at is not None:
                dt = time.perf_counter() - t_now
                if dt > 0.05 and chunk > 16:
                    chunk = max(16, chunk // 4)
                elif dt < 0.005 and chunk < _SLICE:
                    chunk = min(_SLICE, chunk * 4)
            pos = end
    except _ParseDeadline as ex:
        # stopped inside one line (long gate body or long multi-statement line)
        inner = ex.args[0] if ex.args else 0.0
        end = min(pos + chunk, n_lines)
        k = next((i for i in range(pos, end) if lines[i] is line), pos)
        k0 = k
        if pending_gate is not None and pending_first is not None:
            k0 = next((i for i in range(k, -1, -1) if lines[i] is pending_first), k)
        frac = (k0 + inner * (k - k0 + 1)) / n_lines
    if frac is not None:
        return _finalize(top, version, len(text), n_lines, state,
                         scale=1.0 / max(frac, 1.0 / max(n_lines, 1)))
    return _finalize(top, version, len(text), n_lines, state)


def _sum_min(lo, hi, v):
    """sum_{t=lo..hi} min(v, t) for integers lo <= hi (0 if empty), v >= 0."""
    if lo > hi:
        return 0
    top = v if isinstance(v, int) else math.floor(v)
    u = min(hi, top)
    if u < lo:
        return v * (hi - lo + 1)
    return (lo + u) * (u - lo + 1) // 2 + v * (hi - u)


def _cut_stats(diff, L):
    """Cut-crossing stats over cuts 0..L-1 from a sparse difference map.
    Returns (max, mean, central-half max, max capacity, sum capacity) where
    capacity(k) = min(cross(k), k+1, L-k)."""
    if L <= 0:
        return 0, 0.0, 0, 0, 0
    q1 = L // 4
    q3 = max(L * 3 // 4, L // 4 + 1)
    c = (L + 1) // 2  # h(k) = k+1 for k < c, L-k for k >= c

    def h(k):
        return min(k + 1, L - k)

    max_cross = None
    mid_max = None
    max_cap = None
    sum_cross = 0
    sum_cap = 0
    run = 0
    prev = 0
    pts = sorted(k for k in diff if k < L)
    pts.append(L)
    for p in pts:
        if p > prev:
            a, b, v = prev, p, run
            if max_cross is None or v > max_cross:
                max_cross = v
            if a < q3 and b > q1 and (mid_max is None or v > mid_max):
                mid_max = v
            sum_cross += v * (b - a)
            # sum of min(v, h(k)) over the rising and the falling part of the tent
            sum_cap += _sum_min(a + 1, min(b, c), v)
            sum_cap += _sum_min(L - b + 1, L - max(a, c), v)
            if b <= c:
                hmax = h(b - 1)
            elif a >= c:
                hmax = h(a)
            else:
                hmax = max(h(c - 1), h(c))
            cm = min(v, hmax)
            if max_cap is None or cm > max_cap:
                max_cap = cm
        if p < L:
            run += diff[p]
            prev = p
    return max_cross, sum_cross / L, mid_max, max_cap, sum_cap


def _finalize(sc: _Scope, version, n_bytes, n_lines, state, scale=None) -> dict:
    """scale (deadline hit): extrapolate the additive quantities by this factor; qubit,
    clbit, byte and line counts and the distinct-name / degree / max-span stats stay as seen."""
    c = sc.counts
    n = sc.n_q
    lv = sc.level
    n_used = (n - lv.count(0)) if len(lv) == n else sum(1 for i in range(n) if lv[i] > 0)
    depth = max(lv) if lv else 0
    depth2 = max(sc.level2) if sc.level2 else 0
    n_barrier, n_custom = sc.n_barrier, sc.n_custom_calls
    n_if, n_defs, n_pairs = state["n_if"], state["n_gate_defs"], len(sc.pairs)
    if scale is not None:
        c = [x * scale for x in c]
        depth, depth2 = depth * scale, depth2 * scale
        n_barrier, n_custom = n_barrier * scale, n_custom * scale
        n_if, n_defs, n_pairs = n_if * scale, n_defs * scale, n_pairs * scale

    # Interaction statistics. Use a sparse difference map over cuts for "cut crossings":
    # cut k sits between qubit k and k+1; a 2q gate (a,b) crosses cuts a..b-1. The crossing
    # count is constant between endpoints, so all cut stats are computed per segment
    # (O(distinct pairs), not O(n_qubits)); integer results equal the per-cut loop exactly.
    diff = {}
    dist_sum = 0
    dist_max = 0
    n_nonlocal = 0
    n2 = 0
    deg = Counter()
    for (a, b), m in sc.pairs.items():
        if scale is not None:
            m = m * scale
        d = b - a
        diff[a] = diff.get(a, 0) + m
        diff[b] = diff.get(b, 0) - m
        dist_sum += d * m
        n2 += m
        if d > dist_max:
            dist_max = d
        if d > 1:
            n_nonlocal += m
        deg[a] += 1
        deg[b] += 1
    max_cross, mean_cross, mid_max, max_cap, sum_cap = _cut_stats(diff, max(n - 1, 0))

    tot = c[_C_TOTAL]
    f = {
        "qasm_version": version,
        "n_bytes": n_bytes,
        "n_lines": n_lines,
        "n_qubits": n,
        "n_qubits_used": n_used,
        "n_clbits": sc.n_c,
        "n_gates": tot,
        "n_1q": c[_C_1Q],
        "n_2q": c[_C_2Q],
        "n_3q_plus": c[_C_3Q],
        "n_nonclifford": c[_C_NONCLIFF],
        "n_param_gates": c[_C_PARAM],
        "n_measure": c[_C_MEAS],
        "n_reset": c[_C_RESET],
        "n_conditional": c[_C_COND],
        "n_if_blocks": n_if,
        "n_barrier": n_barrier,
        "n_gate_defs": n_defs,
        "n_custom_calls": n_custom,
        "depth": depth,
        "depth_2q": depth2,
        "n_distinct_gates": len(sc.names),
        "n_distinct_pairs": n_pairs,
        "max_degree": max(deg.values()) if deg else 0,
        "mean_degree": (sum(deg.values()) / n_used) if n_used else 0.0,
        "gate_dist_mean": dist_sum / n2 if n2 else 0.0,
        "gate_dist_max": dist_max,
        "frac_nonlocal_2q": n_nonlocal / n2 if n2 else 0.0,
        "cut_cross_max": max_cross,
        "cut_cross_mean": mean_cross,
        "cut_cross_mid_max": mid_max,
        "ent_cap_max": max_cap,
        "ent_cap_sum": sum_cap,
        "frac_2q": c[_C_2Q] / tot if tot else 0.0,
        "frac_nonclifford": c[_C_NONCLIFF] / tot if tot else 0.0,
        "two_q_per_qubit": c[_C_2Q] / n_used if n_used else 0.0,
        "gates_per_layer": tot / depth if depth else 0.0,
    }
    # a few log transforms (trees don't need them, but they help ratio features)
    for k in ("n_gates", "n_2q", "depth", "n_bytes", "cut_cross_max", "ent_cap_sum"):
        f["log_" + k] = math.log10(1 + f[k])
    return f


if __name__ == "__main__":
    import sys
    import time
    from pathlib import Path

    for p in sys.argv[1:]:
        t = time.perf_counter()
        txt = Path(p).read_text(errors="replace")
        t1 = time.perf_counter()
        feats = featurize(txt)
        t2 = time.perf_counter()
        print(f"{Path(p).name}: read {t1-t:.2f}s featurize {t2-t1:.2f}s  "
              f"q={feats['n_qubits']} gates={feats['n_gates']} 2q={feats['n_2q']} "
              f"depth={feats['depth']} cut_max={feats['cut_cross_max']} v={feats['qasm_version']}")
