"""Featurize every circuit in a directory -> features.csv (one row per file).

Usage: python build_features.py --circuits ../quantumrings-challenge-iqc-2026/raw-circuits --out features.csv
Accepts .qasm and .qasm.zst.
Adds the C++ stepper's st_* columns (qasm_step.py); --fallback-frac routes a
deterministic fraction of files through the Python fallback stepper instead.
"""
import argparse
import csv
import functools
import time
import zlib
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from qasm_features import featurize
from qasm_step import finish_step, start_step
from qasm_step_py import py_step


def read_qasm(path: Path) -> str:
    if path.suffix == ".zst":
        import zstandard as zstd
        with open(path, "rb") as f:
            return zstd.ZstdDecompressor().stream_reader(f).read().decode("utf-8", "replace")
    return path.read_text(encoding="utf-8", errors="replace")


def _use_fallback(name, frac):
    return frac > 0 and (zlib.crc32(name.encode()) % 10000) < frac * 10000


def _one(path_str, fallback_frac=0.0):
    p = Path(path_str)
    name = p.name[:-4] if p.suffix == ".zst" else p.name
    use_fb = _use_fallback(name, fallback_frac)
    t = time.perf_counter()
    handle = None
    if not use_fb and p.suffix != ".zst":
        handle = start_step(path_str)
    text = read_qasm(p)
    if not use_fb and handle is None:
        handle = start_step(text)
    f = featurize(text)
    f["parse_s"] = round(time.perf_counter() - t, 3)
    elapsed = time.perf_counter() - t
    hint = f["n_2q"] + f["n_3q_plus"]
    if use_fb:
        st = py_step(text, deadline_s=max(1.0, 12.0 - elapsed), n2q_hint=hint)
    else:
        st = finish_step(handle, timeout_s=max(1.0, 12.0 - elapsed), n2q_hint=hint)
    return {"filename": name, **f, **st}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--circuits", required=True)
    ap.add_argument("--out", default="features.csv")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--fallback-frac", type=float, default=0.03,
                    help="fraction of files (by filename hash) featurized by the Python fallback stepper")
    a = ap.parse_args()
    files = sorted(str(p) for p in Path(a.circuits).iterdir() if p.suffix in (".qasm", ".zst"))
    # largest first so the long poles start early
    files.sort(key=lambda s: -Path(s).stat().st_size)
    with ProcessPoolExecutor(a.workers) as ex:
        rows = list(ex.map(functools.partial(_one, fallback_frac=a.fallback_frac), files, chunksize=1))
    rows.sort(key=lambda r: r["filename"])
    with open(a.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    slow = sorted(rows, key=lambda r: -r["parse_s"])[:5]
    print(f"wrote {len(rows)} rows -> {a.out}; slowest:",
          ", ".join(f"{r['filename']} {r['parse_s']}s" for r in slow))


if __name__ == "__main__":
    main()
