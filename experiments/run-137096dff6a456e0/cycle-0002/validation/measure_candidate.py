"""Measure the approved cycle-0002 candidate using only the public packet.

Reuses historical benchmark inputs and exact timing boundaries without changing
the baseline script or evidence. No scoring, simulation, or data discovery.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import joblib
from model import RuntimeModel
from runtime_pipeline.learning import fit_model
from validation import measure_baseline as helpers

FIXED_CONFIG = {**helpers.FIXED_CONFIG, "criterion": "absolute_error"}
OUTPUT = ROOT / "validation" / "cycle-0002-criterion-timing"
DEADLINE = datetime.fromisoformat("2026-11-07T14:00:00-07:00")
TRACKED_FILES = (*helpers.TRACKED_FILES, "runtime_pipeline/io.py",
                 "runtime_pipeline/__init__.py", "validation/measure_candidate.py",
                 "requirements.lock.txt")


def check_deadline():
    if datetime.now(timezone.utc) >= DEADLINE:
        raise TimeoutError("Controller deadline reached; incomplete timing cannot pass")


def verify_fitted(estimator, seed, row_count):
    """Inspect learned objects, including every tree, rather than trust config."""
    regressor = estimator.model.named_steps["regressor"]
    params = regressor.get_params(deep=False)
    required = {key: value for key, value in FIXED_CONFIG.items() if key != "kind"}
    required.update(random_state=seed, max_depth=None, bootstrap=False)
    if any(params.get(key) != value for key, value in required.items()):
        raise ValueError("Actual estimator settings differ from the approved candidate")
    trees = regressor.estimators_
    if len(trees) != 400 or any(tree.criterion != "absolute_error" for tree in trees):
        raise ValueError("Expected 400 fitted absolute_error trees")
    root_counts = sorted({int(tree.tree_.n_node_samples[0]) for tree in trees})
    if root_counts != [row_count]:
        raise ValueError("Fitted tree root counts do not reconcile with all supplied rows")
    return {"fitted_parameters": params, "fitted_criterion": regressor.criterion,
            "fitted_tree_count": len(trees), "fitted_seed": regressor.random_state,
            "fitted_feature_count": len(estimator.model.named_steps["vectorizer"].vocabulary_),
            "fitted_tree_root_row_counts": root_counts,
            "fitted_tree_criteria": sorted({tree.criterion for tree in trees})}


def fit_timing_models(features, records, config, report, output):
    models = {}
    for seed in helpers.SEEDS:
        row = {"seed": seed, "status": "failed", "rows_supplied": len(records)}
        report["fits"].append(row)
        try:
            check_deadline()
            started = time.perf_counter()
            try:
                estimator = fit_model(features, [r[1] for r in records],
                                      [r[2] for r in records], seed, config)
            finally:
                row["fit_s"] = time.perf_counter() - started
            row.update(verify_fitted(estimator, seed, len(records)))
            directory = output / f"seed-{seed}"
            directory.mkdir()
            artifact = directory / "model.joblib"
            started = time.perf_counter()
            joblib.dump(estimator, artifact, compress=3)
            row["save_s"] = time.perf_counter() - started
            started = time.perf_counter()
            try:
                adapter = RuntimeModel(artifacts_dir=directory)
            finally:
                row["load_s"] = time.perf_counter() - started
            restored = verify_fitted(adapter.model, seed, len(records))
            if restored != {key: row[key] for key in restored}:
                raise ValueError("Fitted settings changed across serialization")
            row.update(status="ok", artifact=str(artifact.relative_to(ROOT)),
                       artifact_bytes=artifact.stat().st_size,
                       artifact_sha256=helpers.sha256(artifact))
            models[seed] = adapter
            print(json.dumps({"fit": row}), flush=True)
        except Exception as exc:
            helpers.failure(report, "timing_fit_or_load", f"seed-{seed}", repr(exc))
        helpers.write_report(report, output)
    return models


def account(report, specs, thresholds):
    # Materialize all planned calls even if setup/training/benchmarking aborts.
    cases = {case["name"]: case for case in report["benchmarks"]}
    for spec in specs:
        if spec["name"] not in cases:
            cases[spec["name"]] = {**spec, "trials": []}
            report["benchmarks"].append(cases[spec["name"]])
        case = cases[spec["name"]]
        trials = {trial["repeat"]: trial for trial in case["trials"]}
        for repeat in range(1, helpers.REPEATS + 1):
            if repeat not in trials:
                trials[repeat] = {"repeat": repeat, "parser_status": "not_attempted",
                                  "reason": "Measurement interrupted before call", "predictions": []}
                case["trials"].append(trials[repeat])
            trial = trials[repeat]
            present = {(p["seed"], p["threshold"]) for p in trial["predictions"]}
            for seed in helpers.SEEDS:
                for threshold in thresholds:
                    if (seed, threshold) not in present:
                        trial["predictions"].append({"seed": seed, "threshold": threshold,
                            "status": "not_attempted", "reason": "Measurement interrupted before call"})
    trials = [t for case in report["benchmarks"] for t in case["trials"]]
    predictions = [p for t in trials for p in t["predictions"]]
    return {
        "benchmark_cases": len(report["benchmarks"]),
        "parser_calls_attempted": sum(t["parser_status"] != "not_attempted" for t in trials),
        "parser_calls_ok": sum(t["parser_status"] == "ok" for t in trials),
        "parser_calls_not_attempted": sum(t["parser_status"] == "not_attempted" for t in trials),
        "predictions_attempted": sum(p["status"] != "not_attempted" for p in predictions),
        "predictions_ok": sum(p["status"] == "ok" for p in predictions),
        "predictions_not_attempted": sum(p["status"] == "not_attempted" for p in predictions),
        "first_predictions_recorded": sum(p.get("first_call_for_seed", False) for p in predictions),
        "max_benchmark_parser_s": max((t["parser_s"] for t in trials if "parser_s" in t), default=None),
        "max_training_parser_s": max((r["parser_s"] for r in report["training"]["parses"]
                                      if "parser_s" in r), default=None),
        "max_singleton_predict_s": max((p["elapsed_s"] for p in predictions if "elapsed_s" in p), default=None),
    }


def main():
    args_parser = argparse.ArgumentParser(description=__doc__)
    args_parser.add_argument("--development-inputs", required=True, type=Path)
    args = args_parser.parse_args()
    output = OUTPUT.resolve()
    if not output.is_relative_to(ROOT / "validation"):
        raise ValueError("Timing output must remain candidate-local")
    output.mkdir(parents=True, exist_ok=True)
    if (output / "report.json").exists() or any(output.glob("seed-*")):
        raise FileExistsError("Refusing to overwrite existing candidate timing evidence")
    started = time.perf_counter()
    report = {
        "run_id": "iqc-2026-runtime", "cycle_id": "cycle-0002",
        "step_id": "step-02-attempt-0-code", "role": "coder",
        "manager_step_id": "criterion-03-candidate-timing",
        "started_at": datetime.now(timezone.utc).isoformat(), "status": "running",
        "deadline": DEADLINE.isoformat(),
        "seconds_until_deadline_at_start": (DEADLINE - datetime.now(timezone.utc)).total_seconds(),
        "command_argv": [sys.executable, *sys.argv], "working_directory": str(Path.cwd()),
        "environment": {"python": sys.version, "platform": platform.platform(),
                        "cpu_count": os.cpu_count(), "versions": {
                            name: importlib.metadata.version(name) for name in
                            ("numpy", "scikit-learn", "joblib", "zstandard", "threadpoolctl")}},
        "development_inputs": str(args.development_inputs.resolve()),
        "seeds": helpers.SEEDS, "repeats": helpers.REPEATS,
        "parser_cap_s": helpers.CALL_CAP_S, "predict_cap_s": helpers.CALL_CAP_S,
        "timing_boundaries": "perf_counter around each RuntimeModel.featurize(text) and singleton "
            "RuntimeModel.predict(features, threshold), including first calls. Reads/decompression, "
            "input construction, checks, evidence writes, fit/save/load are excluded. Training "
            "parser.featurize calls and sequential fits are measured separately.",
        "limitations": ["Local timing only; no accuracy measurement or universal latency guarantee.",
            "Three repeats without discarded warmups; training parses and fits precede benchmarks.",
            "Only supplied fold-0 training labels are fitted; benchmark rows add no fitting data.",
            "Circuit paths/names and hashes are joins/provenance only, never inference features.",
            "Parser features retain existing sampling/approximation semantics.",
            "The 15-second limits apply separately to parser and prediction, not fitting.",
            "Deadline checked between stages; in-progress calls are not forcibly interrupted."],
        "training": {"parses": []}, "fits": [], "benchmarks": [], "failures": [],
        "source_sha256_before": {name: helpers.sha256(ROOT / name) for name in TRACKED_FILES},
    }
    helpers.write_report(report, output)
    specs, thresholds, models, input_paths = [], [], {}, {}
    try:
        check_deadline()
        packet = json.loads(args.development_inputs.read_text(encoding="utf-8-sig"))
        if packet["training_fold"] != 0:
            raise ValueError("Expected fold-0 training packet")
        config = json.loads((ROOT / "model_config.json").read_text(encoding="utf-8-sig"))
        report["config"] = config
        if config != FIXED_CONFIG:
            raise ValueError("Configuration differs from the exact manager-approved candidate")
        records = helpers.load_training(packet)
        thresholds = sorted({r[1] for r in records})
        report["thresholds"] = thresholds
        specs = [{**s, "source": "synthetic"} for s in helpers.synthetic_specs()]
        specs += [{"name": name, "source": "designated_development", "path": path}
                  for name, path in packet["benchmark_circuits"].items()]
        report["expected"] = {"benchmark_cases": len(specs),
            "parser_calls": len(specs) * helpers.REPEATS,
            "singleton_predictions": len(specs) * helpers.REPEATS * len(helpers.SEEDS) * len(thresholds)}
        names = {r[0] for r in records}
        report["training"].update(labels_path=packet["training_labels"], fold=0,
            label_rows=len(records), circuit_count=len(names),
            packet_circuit_count=len(packet["training_circuits"]),
            rows_per_threshold=dict(Counter(str(r[1]) for r in records)),
            fitting_policy="Every supplied training row exactly once; no benchmark/synthetic rows added")
        if names != set(packet["training_circuits"]) or len(names) != 340 or len(records) != 1692:
            raise ValueError("Packet/label training counts differ from approved 1692 rows / 340 circuits")
        if len(packet["benchmark_circuits"]) != 8 or report["expected"] != {
                "benchmark_cases": 21, "parser_calls": 63, "singleton_predictions": 630}:
            raise ValueError("Benchmark coverage differs from the approved packet")
        input_paths = {"packet": args.development_inputs, "labels": Path(packet["training_labels"])}
        input_paths.update({f"training/{n}": Path(p) for n, p in packet["training_circuits"].items()})
        input_paths.update({f"benchmark/{n}": Path(p) for n, p in packet["benchmark_circuits"].items()})
        report["input_sha256_before"] = {name: helpers.sha256(path) for name, path in input_paths.items()}
        features = helpers.training_features(records, packet, report, output)
        models = fit_timing_models(features, records, config, report, output)
        first_prediction = set()
        for spec in specs:
            check_deadline()
            helpers.benchmark_case(spec, thresholds, models, report, output, first_prediction)
        check_deadline()
    except Exception as exc:
        helpers.failure(report, "setup_or_measurement", "candidate", repr(exc))
    report["accounting"] = account(report, specs, thresholds)
    for kind, paths in (("source", {n: ROOT / n for n in TRACKED_FILES}), ("input", input_paths)):
        try:
            report[f"{kind}_sha256_after"] = {n: helpers.sha256(p) for n, p in paths.items()}
            if report[f"{kind}_sha256_after"] != report.get(f"{kind}_sha256_before"):
                raise ValueError(f"Measured {kind} hashes changed or initial hashing was incomplete")
        except Exception as exc:
            helpers.failure(report, "integrity", kind, repr(exc))
    accounting = report["accounting"]
    complete = (len(models) == 2 and accounting["parser_calls_ok"] == 63
                and accounting["predictions_ok"] == 630
                and accounting["first_predictions_recorded"] == 2)
    report["status"] = "passed" if complete and not report["failures"] else "failed"
    report["elapsed_s"] = time.perf_counter() - started
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    helpers.write_report(report, output)
    print(json.dumps({"status": report["status"], "accounting": accounting,
                      "failures": len(report["failures"])}), flush=True)
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
