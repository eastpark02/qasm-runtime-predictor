"""Featurize every circuit in a directory -> features.csv (one row per file).

Usage: python build_features.py --circuits ../quantumrings-challenge-iqc-2026/raw-circuits --out features.csv
Accepts .qasm and .qasm.zst.
"""
import argparse
import csv
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from qasm_features import featurize


def read_qasm(path: Path) -> str:
    if path.suffix == ".zst":
        import zstandard as zstd
        with open(path, "rb") as f:
            return zstd.ZstdDecompressor().stream_reader(f).read().decode("utf-8", "replace")
    return path.read_text(encoding="utf-8", errors="replace")


def _one(path_str):
    p = Path(path_str)
    name = p.name[:-4] if p.suffix == ".zst" else p.name
    t = time.perf_counter()
    f = featurize(read_qasm(p))
    f["parse_s"] = round(time.perf_counter() - t, 3)
    return {"filename": name, **f}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--circuits", required=True)
    ap.add_argument("--out", default="features.csv")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    files = sorted(str(p) for p in Path(a.circuits).iterdir() if p.suffix in (".qasm", ".zst"))
    # largest first so the long poles start early
    files.sort(key=lambda s: -Path(s).stat().st_size)
    with ProcessPoolExecutor(a.workers) as ex:
        rows = list(ex.map(_one, files, chunksize=1))
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
