"""
Subprocess wrapper around the C++ moment stepper (stepper/qasm_step[.exe]).

    h = start_step(path_or_text)       # spawns the binary, returns immediately
    ...                                 # run qasm_features.featurize meanwhile
    st = finish_step(h, timeout_s)      # dict with exactly ST_COLS keys

Any failure (no binary, nonzero exit, timeout, bad JSON, missing/non-finite keys)
kills the process, removes the temp file, and returns the pure-Python fallback
(qasm_step_py.py). Never raises, never returns NaN.
st_ok: 1 = binary, 0.75 = fallback, 0.5 = fallback hit its deadline.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from qasm_step_py import DEFAULT_THRESHOLDS, py_step, st_cols  # noqa: E402

ST_COLS = st_cols(DEFAULT_THRESHOLDS)
# total per-circuit budget the fallback may use, counted from start_step()
_BUDGET_S = 13.0


def find_binary():
    exe = "qasm_step.exe" if sys.platform.startswith("win") else "qasm_step"
    for d in (os.path.join(HERE, "stepper"),
              os.path.join(HERE, "stepper", "build", "Release"),
              os.path.join(HERE, "stepper", "build"),
              HERE):
        p = os.path.join(d, exe)
        if os.path.isfile(p) and (sys.platform.startswith("win") or os.access(p, os.X_OK)):
            return p
    return None


class _Handle:
    __slots__ = ("proc", "path", "tmp", "text", "t0", "thresholds")


def _is_path(x):
    if isinstance(x, os.PathLike):
        return True
    if not isinstance(x, str) or len(x) > 4096 or "\n" in x or ";" in x:
        return False
    try:
        return os.path.isfile(x)
    except (OSError, ValueError):
        return False


def start_step(path_or_text, thresholds=None) -> _Handle:
    h = _Handle()
    h.t0 = time.perf_counter()
    h.proc = None
    h.tmp = False
    h.text = None
    h.path = None
    h.thresholds = tuple(thresholds) if thresholds else DEFAULT_THRESHOLDS
    try:
        if _is_path(path_or_text):
            h.path = os.fspath(path_or_text)
        else:
            h.text = path_or_text
            f = tempfile.NamedTemporaryFile(delete=False, suffix=".qasm", mode="wb")
            try:
                f.write(path_or_text.encode("utf-8", errors="replace"))
            finally:
                f.close()  # must be closed before the child opens it (Windows)
            h.path, h.tmp = f.name, True
        exe = find_binary()
        if exe is None:
            return h
        cmd = [exe]
        if thresholds:
            cmd += ["--thresholds", ",".join(str(int(t)) for t in h.thresholds)]
        cmd.append(h.path)
        h.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL)
    except Exception:
        h.proc = None
    return h


def _parse(out: bytes, cols):
    lines = [ln for ln in out.decode("utf-8", errors="replace").splitlines() if ln.strip()]
    if not lines:
        return None
    d = json.loads(lines[-1])
    if not isinstance(d, dict):
        return None
    res = {}
    for k in cols:
        v = float(d[k])
        if not math.isfinite(v):
            return None
        res[k] = v
    return res


def _cleanup(h):
    if h.tmp and h.path:
        try:
            os.remove(h.path)
        except OSError:
            pass
        h.tmp = False


def finish_step(h: _Handle, timeout_s: float = 12.0, n2q_hint=None) -> dict:
    cols = st_cols(h.thresholds)
    res = None
    try:
        if h.proc is not None:
            try:
                out, _ = h.proc.communicate(timeout=max(0.0, timeout_s))
                if h.proc.returncode == 0:
                    res = _parse(out, cols)
            except subprocess.TimeoutExpired:
                pass
            except Exception:
                res = None
            finally:
                if h.proc.poll() is None:
                    try:
                        h.proc.kill()
                        h.proc.communicate(timeout=2)
                    except Exception:
                        pass
        if res is not None:
            return res
        # ---- fallback ----
        text = h.text
        if text is None:
            try:
                with open(h.path, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except Exception:
                text = ""
        _cleanup(h)
        remaining = _BUDGET_S - (time.perf_counter() - h.t0)
        return py_step(text, deadline_s=max(0.5, remaining), n2q_hint=n2q_hint,
                       thresholds=h.thresholds)
    except Exception:
        return dict.fromkeys(cols, 0.0)
    finally:
        _cleanup(h)


def step_features(path_or_text, deadline_s: float = 12.0, n2q_hint=None) -> dict:
    return finish_step(start_step(path_or_text), timeout_s=deadline_s, n2q_hint=n2q_hint)


if __name__ == "__main__":
    for p in sys.argv[1:]:
        t = time.perf_counter()
        r = step_features(p)
        print(f"{os.path.basename(p)} {time.perf_counter() - t:.2f}s bin={find_binary()}",
              json.dumps(r))
