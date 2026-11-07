"""Candidate-local latency evidence; no scoring, simulation, or data discovery.

Only the explicit public packet's label file and circuit mappings are read.
Names/paths label evidence and join training rows; they never enter fit inputs.
"""

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import joblib

from model import RuntimeModel
from runtime_pipeline import parser
from runtime_pipeline.io import read_qasm
from runtime_pipeline.learning import CAP_SECONDS, MIN_SECONDS, fit_model

SEEDS = (42, 43)
REPEATS = 3
CALL_CAP_S = 15.0
FIXED_CONFIG = {"kind": "extra_trees", "n_estimators": 400,
                "min_samples_leaf": 2, "max_features": 1.0, "n_jobs": 4}
TRACKED_FILES = ("run.py", "score.py", "model.py", "model_config.json",
                 "runtime_pipeline/model.py", "runtime_pipeline/learning.py",
                 "runtime_pipeline/parser.py", "validation/measure_baseline.py")


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_report(report, output):
    (output / "report.json").write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def failure(report, stage, case, error, **details):
    record = {"stage": stage, "case": case, "error": str(error), **details}
    report["failures"].append(record)
    print(json.dumps({"failure": record}), flush=True)


def checked_features(features):
    if not features or not all(isinstance(v, (int, float)) and math.isfinite(v)
                               for v in features.values()):
        raise ValueError("Parser did not return a nonempty finite numeric dictionary")


def input_sizes(text):
    # Count replacement lengths without building another large stripped string.
    removed = sum(m.end() - m.start() - 1 for m in parser._COMMENTS.finditer(text)) \
        if "//" in text or "/*" in text else 0
    return {"text_characters": len(text),
            "comment_stripped_characters": len(text) - removed}


def load_training(packet):
    with Path(packet["training_labels"]).open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    records = []
    seen = set()
    for row in rows:
        name = row["filename"].strip()
        threshold = float(row["threshold"])
        status = row["status"].strip().lower()
        target = CAP_SECONDS if status == "timeout" else float(row["duration_s"])
        if status not in {"success", "timeout"} or not all(
                math.isfinite(v) and v > 0 for v in (threshold, target)):
            raise ValueError("Invalid supplied training row")
        if name not in packet["training_circuits"] or (name, threshold) in seen:
            raise ValueError("Missing supplied training circuit or duplicate row")
        seen.add((name, threshold))
        records.append((name, threshold, target))
    if not records:
        raise ValueError("The supplied training fold is empty")
    return records


def synthetic_specs():
    specs = [{"name": f"builtin-broadcast-{width}", "kind": "broadcast", "width": width}
             for width in (25000, 25001, 100000)]
    specs += [{"name": "custom-broadcast-100000", "kind": "custom-broadcast", "width": 100000},
              {"name": "isolated-qubits-100000", "kind": "isolated"}]
    specs += [{"name": f"dependency-chars-{size}", "kind": "dependency", "size": size}
              for size in (1499999, 1500000, 1500001)]
    specs += [{"name": f"histogram-chars-{size}", "kind": "histogram", "size": size}
              for size in (7999999, 8000000, 8000001)]
    specs += [{"name": "large-comment-small-stripped-body", "kind": "comment"},
              {"name": "static-branches-and-loop", "kind": "control"}]
    return specs


def synthetic_text(spec):
    kind = spec["kind"]
    if kind in {"broadcast", "custom-broadcast"}:
        width = spec["width"]
        definition = "gate pair a,b { h a; cx a,b; } " if kind == "custom-broadcast" else ""
        gate = "pair" if definition else "cx"
        return f"OPENQASM 2.0; {definition}qreg a[{width}]; qreg b[{width}]; {gate} a,b;"
    if kind == "isolated":
        return "OPENQASM 2.0; qreg q[100000]; h q[0];"
    if kind == "comment":
        return "OPENQASM 2.0; qreg q[2]; /*" + "x" * 8000000 + "*/ h q[0];"
    if kind == "control":
        return ("OPENQASM 3.0; qubit[2] q; bit c; "
                "if(c) { x q[0]; } else { h q[1]; } "
                "for int i in [0:999999] { cx q[0],q[1]; }")
    if kind == "dependency":
        prefix = "OPENQASM 2.0; qreg q[2];\n"
        # Fewer than 25,000 instances: exercise the character boundary itself.
        line = "h q[0];" + " " * 57 + "\n"
    else:
        prefix = ("OPENQASM 2.0; qreg q[3];\n"
                  "gate pair a,b { h a; cx a,b; }\ngate unused a { t a; }\n")
        line = "pair q[0],q[1];\n"
    size = spec["size"]
    return (prefix + line * ((size - len(prefix)) // len(line))).ljust(size)


def training_features(records, packet, report, output):
    cache = {}
    names = sorted({name for name, _, _ in records})
    for index, name in enumerate(names, 1):
        path = Path(packet["training_circuits"][name])
        row = {"name": name, "status": "failed"}
        report["training"]["parses"].append(row)
        try:
            text = read_qasm(path)
            row.update(input_sizes(text), input_file_bytes=path.stat().st_size)
        except Exception as exc:
            failure(report, "training_read", name, repr(exc))
            continue
        started = time.perf_counter()
        try:
            features = parser.featurize(text)
        except Exception as exc:
            row["parser_s"] = time.perf_counter() - started
            failure(report, "training_parser", name, repr(exc), elapsed_s=row["parser_s"])
        else:
            row["parser_s"] = time.perf_counter() - started
            try:
                checked_features(features)
                cache[name] = features
                row.update(status="ok", feature_count=len(features),
                           n_qubits=features["n_qubits"], n_ops=features["n_ops"],
                           expanded_counts_complete=features["expanded_counts_complete"],
                           within_parser_cap=row["parser_s"] <= CALL_CAP_S)
                if not row["within_parser_cap"]:
                    failure(report, "training_parser_cap", name, "Exceeded 15 seconds",
                            elapsed_s=row["parser_s"])
            except Exception as exc:
                failure(report, "training_feature_contract", name, repr(exc))
        del text
        if index % 25 == 0 or index == len(names):
            write_report(report, output)
            print(json.dumps({"training_parses_done": index, "expected": len(names)}), flush=True)
    report["training"]["successful_parses"] = len(cache)
    if len(cache) != len(names):
        raise ValueError("Incomplete training features; no partial training fit permitted")
    return [cache[name] for name, _, _ in records]


def fit_timing_models(features, records, config, report, output):
    models = {}
    for seed in SEEDS:
        row = {"seed": seed, "status": "failed"}
        report["fits"].append(row)
        try:
            started = time.perf_counter()
            estimator = fit_model(features, [r[1] for r in records], [r[2] for r in records], seed, config)
            row["fit_s"] = time.perf_counter() - started
            directory = output / f"seed-{seed}"
            directory.mkdir(exist_ok=True)
            artifact = directory / "model.joblib"
            joblib.dump(estimator, artifact, compress=3)
            started = time.perf_counter()
            adapter = RuntimeModel(artifacts_dir=directory)
            row["load_s"] = time.perf_counter() - started
            regressor = adapter.model.model.named_steps["regressor"]
            row.update(status="ok", artifact=str(artifact.relative_to(ROOT)),
                       artifact_bytes=artifact.stat().st_size, artifact_sha256=sha256(artifact),
                       fitted_tree_count=len(regressor.estimators_),
                       fitted_seed=regressor.random_state,
                       fitted_feature_count=len(adapter.model.model.named_steps["vectorizer"].vocabulary_))
            models[seed] = adapter
            print(json.dumps({"fit": row}), flush=True)
        except Exception as exc:
            failure(report, "timing_fit_or_load", f"seed-{seed}", repr(exc))
        write_report(report, output)
    return models


def benchmark_case(spec, thresholds, models, report, output, first_prediction):
    case = {**spec, "trials": []}
    report["benchmarks"].append(case)
    text = None
    try:
        if spec["source"] == "synthetic":
            text = synthetic_text(spec)
        else:
            path = Path(spec["path"])
            text = read_qasm(path)
            case["input_file_bytes"] = path.stat().st_size
        case.update(input_sizes(text))
    except Exception as exc:
        failure(report, "benchmark_input", spec["name"], repr(exc))
    # Use the root adapter for parser timing, as in the official harness.
    adapter = next(iter(models.values()), None)
    for repeat in range(1, REPEATS + 1):
        trial = {"repeat": repeat, "parser_status": "not_attempted", "predictions": []}
        case["trials"].append(trial)
        features = None
        if text is not None and adapter is not None:
            started = time.perf_counter()
            try:
                features = adapter.featurize(text)
            except Exception as exc:
                trial.update(parser_s=time.perf_counter() - started, parser_status="failed")
                failure(report, "benchmark_parser", spec["name"], repr(exc), repeat=repeat)
            else:
                trial.update(parser_s=time.perf_counter() - started, parser_status="ok")
                trial["within_parser_cap"] = trial["parser_s"] <= CALL_CAP_S
                if not trial["within_parser_cap"]:
                    failure(report, "benchmark_parser_cap", spec["name"], "Exceeded 15 seconds",
                            repeat=repeat, elapsed_s=trial["parser_s"])
                try:
                    checked_features(features)
                    if "features" in case and case["features"] != features:
                        raise ValueError("Parser features changed between repeats")
                    case["features"] = features
                    case["feature_count"] = len(features)
                except Exception as exc:
                    trial["parser_status"] = "failed"
                    features = None
                    failure(report, "benchmark_feature_contract", spec["name"], repr(exc), repeat=repeat)
        for seed in SEEDS:
            for threshold in thresholds:
                prediction = {"seed": seed, "threshold": threshold, "status": "not_attempted"}
                trial["predictions"].append(prediction)
                if features is None or seed not in models:
                    prediction["reason"] = "Parser input/features or timing model unavailable"
                    continue
                prediction["first_call_for_seed"] = seed not in first_prediction
                first_prediction.add(seed)
                started = time.perf_counter()
                try:
                    seconds = models[seed].predict(features, threshold)
                except Exception as exc:
                    prediction.update(elapsed_s=time.perf_counter() - started, status="failed")
                    failure(report, "singleton_prediction", spec["name"], repr(exc),
                            seed=seed, threshold=threshold, repeat=repeat)
                    continue
                prediction.update(elapsed_s=time.perf_counter() - started, status="ok")
                prediction["within_predict_cap"] = prediction["elapsed_s"] <= CALL_CAP_S
                prediction["finite_positive_bounded"] = (
                    math.isfinite(seconds) and MIN_SECONDS <= seconds <= CAP_SECONDS)
                prediction["seconds"] = seconds if math.isfinite(seconds) else None
                if not prediction["finite_positive_bounded"] or not prediction["within_predict_cap"]:
                    prediction["status"] = "failed"
                    failure(report, "prediction_cap_or_bounds", spec["name"], "Prediction check failed",
                            seed=seed, threshold=threshold, repeat=repeat, result=prediction.copy())
        write_report(report, output)
        print(json.dumps({"case": spec["name"], "repeat": repeat,
                          "parser_s": trial.get("parser_s"),
                          "predictions_ok": sum(p["status"] == "ok" for p in trial["predictions"])}), flush=True)


def main():
    args_parser = argparse.ArgumentParser(description=__doc__)
    args_parser.add_argument("--development-inputs", required=True, type=Path)
    args_parser.add_argument("--output", type=Path, default=ROOT / "validation" / "baseline-03-timing")
    args = args_parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / "validation"):
        raise ValueError("Timing output must remain in the candidate validation directory")
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "run_id": "iqc-2026-runtime", "cycle_id": "cycle-0001",
        "step_id": "step-02-attempt-0-code", "manager_step_id": "baseline-03-document-and-measure",
        "started_at": datetime.now(timezone.utc).isoformat(), "status": "running",
        "command_argv": [sys.executable, *sys.argv], "working_directory": str(Path.cwd()),
        "environment": {"python": sys.version, "platform": platform.platform(),
                        "cpu_count": os.cpu_count(), "versions": {
                            name: importlib.metadata.version(name) for name in
                            ("numpy", "scikit-learn", "joblib", "zstandard", "threadpoolctl")}},
        "development_inputs": str(args.development_inputs.resolve()),
        "seeds": SEEDS, "repeats": REPEATS, "parser_cap_s": CALL_CAP_S, "predict_cap_s": CALL_CAP_S,
        "timing_boundaries": "perf_counter around each adapter.featurize(text) and each singleton "
                             "adapter.predict(features, threshold). Excludes reads/decompression, input "
                             "construction, model fitting/loading, checks, and evidence writing. "
                             "Training feature calls are timed separately around parser.featurize(text).",
        "limitations": ["Local wall-clock measurements, not a worst-case guarantee for all QASM.",
                        "Three repeats, no discarded warmup calls. Fits and training parses precede benchmarks.",
                        "Only the public packet training fold is fitted; no validation scoring or audit access.",
                        "Circuit names and paths are joins/evidence only; no timing cache is an inference input."],
        "training": {"parses": []}, "fits": [], "benchmarks": [], "failures": [],
        "source_sha256_before": {name: sha256(ROOT / name) for name in TRACKED_FILES},
    }
    write_report(report, output)
    started = time.perf_counter()
    try:
        packet = json.loads(args.development_inputs.read_text(encoding="utf-8-sig"))
        if packet["training_fold"] != 0:
            raise ValueError("Expected the controller-designated fold-0 training packet")
        config = json.loads((ROOT / "model_config.json").read_text(encoding="utf-8-sig"))
        if config != FIXED_CONFIG:
            raise ValueError("Configuration differs from the manager-approved fixed baseline")
        report["config"] = config
        records = load_training(packet)
        thresholds = sorted({threshold for _, threshold, _ in records})
        report["thresholds"] = thresholds
        report["training"].update(labels_path=packet["training_labels"], fold=packet["training_fold"],
                                  label_rows=len(records),
                                  circuit_count=len({r[0] for r in records}),
                                  packet_circuit_count=len(packet["training_circuits"]),
                                  fitting_policy="Every supplied training row; no benchmark/synthetic rows added")
        specs = [{**spec, "source": "synthetic"} for spec in synthetic_specs()]
        specs += [{"name": name, "source": "designated_development", "path": path}
                  for name, path in packet["benchmark_circuits"].items()]
        report["expected"] = {"benchmark_cases": len(specs), "parser_calls": len(specs) * REPEATS,
                              "singleton_predictions": len(specs) * REPEATS * len(SEEDS) * len(thresholds)}
        features = training_features(records, packet, report, output)
        models = fit_timing_models(features, records, config, report, output)
        first_prediction = set()
        for spec in specs:
            benchmark_case(spec, thresholds, models, report, output, first_prediction)
    except Exception as exc:
        failure(report, "setup_or_training", "measurement", repr(exc))
    trials = [trial for case in report["benchmarks"] for trial in case["trials"]]
    predictions = [p for trial in trials for p in trial["predictions"]]
    report["accounting"] = {
        "benchmark_cases": len(report["benchmarks"]),
        "parser_calls_attempted": sum(t["parser_status"] != "not_attempted" for t in trials),
        "parser_calls_ok": sum(t["parser_status"] == "ok" for t in trials),
        "predictions_attempted": sum(p["status"] != "not_attempted" for p in predictions),
        "predictions_ok": sum(p["status"] == "ok" for p in predictions),
        "predictions_not_attempted": sum(p["status"] == "not_attempted" for p in predictions),
        "max_benchmark_parser_s": max((t["parser_s"] for t in trials if "parser_s" in t), default=None),
        "max_training_parser_s": max((r["parser_s"] for r in report["training"]["parses"]
                                      if "parser_s" in r), default=None),
        "max_singleton_predict_s": max((p["elapsed_s"] for p in predictions if "elapsed_s" in p), default=None),
    }
    expected = report.get("expected", {})
    complete = (len(models) == len(SEEDS) if "models" in locals() else False) and (
        report["accounting"]["parser_calls_ok"] == expected.get("parser_calls") and
        report["accounting"]["predictions_ok"] == expected.get("singleton_predictions"))
    report["source_sha256_after"] = {name: sha256(ROOT / name) for name in TRACKED_FILES}
    if report["source_sha256_after"] != report["source_sha256_before"]:
        failure(report, "source_integrity", "candidate", "A measured source changed during the run")
    report["status"] = "passed" if complete and not report["failures"] else "failed"
    report["elapsed_s"] = time.perf_counter() - started
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_report(report, output)
    print(json.dumps({"status": report["status"], "accounting": report["accounting"],
                      "failures": len(report["failures"])}), flush=True)
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
