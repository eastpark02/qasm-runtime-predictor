"""Local training, inspection, and single-circuit prediction commands."""

import argparse
import csv
import json
from pathlib import Path
import time

from .io import circuit_name, read_qasm
from .parser import featurize


def _circuit_paths(directory: Path):
    paths = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file() and (path.name.endswith(".qasm") or path.name.endswith(".qasm.zst")):
            name = circuit_name(path)
            if name in paths:
                raise ValueError(f"Duplicate raw/compressed circuit: {name}")
            paths[name] = path
    if not paths:
        raise ValueError(f"No circuits found in {directory}")
    return paths


def _labels(path: Path):
    records = {}
    with path.open(newline="", encoding="utf-8-sig") as stream:
        for row in csv.DictReader(stream):
            key = (row["filename"].strip(), int(float(row["threshold"])))
            if key not in records or row.get("status", "").strip().lower() == "success":
                records[key] = row
    result = []
    for (name, threshold), row in records.items():
        if row.get("status", "").strip().lower() == "timeout":
            duration = 21600.0
        elif row.get("duration_s", "").strip():
            duration = float(row["duration_s"])
        else:
            continue
        if duration > 0:
            result.append((name, threshold, duration))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    features = commands.add_parser("features", help="Inspect a circuit's structural features")
    features.add_argument("--qasm", type=Path, required=True)
    prediction = commands.add_parser("predict", help="Predict seconds for one circuit/threshold")
    prediction.add_argument("--qasm", type=Path, required=True)
    prediction.add_argument("--threshold", type=int, required=True)
    prediction.add_argument("--artifacts", type=Path, default=Path("artifacts"))
    training = commands.add_parser("train", help="Fit all supplied labels; this is not validation")
    training.add_argument("--circuits", type=Path, required=True)
    training.add_argument("--labels", type=Path, required=True)
    training.add_argument("--config", type=Path, default=Path("model_config.json"))
    training.add_argument("--artifacts", type=Path, default=Path("artifacts"))
    training.add_argument("--seed", type=int, default=42)
    benchmark = commands.add_parser("benchmark", help="Measure parser latency, without simulation")
    benchmark.add_argument("--circuits", type=Path, required=True)
    benchmark.add_argument("--largest", type=int, default=0, help="Use N largest files; 0 means all")
    args = parser.parse_args(argv)
    if args.command == "features":
        print(json.dumps(featurize(read_qasm(args.qasm)), indent=2, allow_nan=False))
    elif args.command == "predict":
        from .model import RuntimeModel
        model = RuntimeModel(args.artifacts)
        start = time.perf_counter()
        features = model.featurize(read_qasm(args.qasm))
        parsed = time.perf_counter()
        duration = model.predict(features, args.threshold)
        print(json.dumps({"pred_duration_s": duration, "read_and_parse_s": parsed - start,
                          "predict_s": time.perf_counter() - parsed}, indent=2))
    elif args.command == "train":
        import joblib
        from .learning import fit_model
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        paths = _circuit_paths(args.circuits)
        labels = _labels(args.labels)
        names = sorted({row[0] for row in labels})
        missing = set(names) - paths.keys()
        if missing:
            raise ValueError(f"Labeled circuits missing: {sorted(missing)[:5]}")
        cache = {}
        for index, name in enumerate(names, 1):
            cache[name] = featurize(read_qasm(paths[name]))
            if index % 25 == 0:
                print(f"Featurized {index}/{len(names)}", flush=True)
        estimator = fit_model([cache[name] for name, _, _ in labels],
                              [threshold for _, threshold, _ in labels],
                              [duration for _, _, duration in labels], args.seed, config)
        args.artifacts.mkdir(parents=True, exist_ok=True)
        joblib.dump(estimator, args.artifacts / "model.joblib")
        (args.artifacts / "training_metadata.json").write_text(json.dumps({
            "config": config, "seed": args.seed, "circuits": len(names),
            "training_rows": len(labels), "validation_score": None,
            "note": "Full supplied-data fit; use the MAS evaluator for honest validation.",
        }, indent=2), encoding="utf-8")
        print(f"Saved {args.artifacts / 'model.joblib'}")
    elif args.command == "benchmark":
        paths = list(_circuit_paths(args.circuits).values())
        paths.sort(key=lambda path: path.stat().st_size, reverse=True)
        if args.largest:
            paths = paths[:args.largest]
        timings = []
        for path in paths:
            text = read_qasm(path)
            start = time.perf_counter()
            features = featurize(text)
            elapsed = time.perf_counter() - start
            row = {"circuit": circuit_name(path), "parse_s": elapsed,
                   "n_qubits": features["n_qubits"], "n_ops": features["n_ops"]}
            print(json.dumps(row), flush=True)
            timings.append(elapsed)
        print(json.dumps({"circuits": len(timings), "max_parse_s": max(timings),
                          "over_15s": sum(t > 15 for t in timings)}))
        if any(t > 15 for t in timings):
            raise SystemExit(2)


if __name__ == "__main__":
    main()
