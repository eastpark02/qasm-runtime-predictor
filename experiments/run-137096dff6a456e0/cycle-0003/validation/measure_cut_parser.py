"""Parser-only evidence from synthetic fixtures and the public benchmark packet.

No labels, models, scores, circuit simulation, or directory discovery are used.
Input construction and decompression are outside the featurize call timer.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime_pipeline import parser
from runtime_pipeline.io import read_qasm
from validation.cut_fixtures import regression_fixtures
from validation.measure_baseline import checked_features


def main():
    argument_parser = argparse.ArgumentParser(description=__doc__)
    argument_parser.add_argument("--development-inputs", type=Path, required=True)
    args = argument_parser.parse_args()
    packet = json.loads(args.development_inputs.read_text(encoding="utf-8"))
    before = json.loads((ROOT / "validation/cut-statistics-before.json").read_text(encoding="utf-8"))
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "command": [sys.executable, *sys.argv],
        "python": platform.python_version(),
        "platform": platform.platform(),
        "scope": "Parser only; synthetic fixtures and packet benchmark_circuits; no labels or fits.",
        "call_cap_s": 15.0,
        "repeats": 3,
        "detail_limits": {"characters": parser._MAX_DETAIL_CHARS,
                          "operation_instances": parser._MAX_DETAIL_OPS},
        "sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
                   for p in before["sha256"]},
        "calls": [],
        "failures": [],
    }
    report["protected_files_unchanged"] = all(
        value == report["sha256"][path] for path, value in before["sha256"].items()
        if path != "runtime_pipeline/parser.py")
    if not report["protected_files_unchanged"]:
        report["failures"].append({"stage": "integrity", "error": "Protected file changed"})
    output = ROOT / "validation/cut-statistics-timing.json"

    def save():
        output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")

    def measure(name, text, source):
        for repeat in range(report["repeats"]):
            call = {"fixture": name, "source": source, "repeat": repeat,
                    "characters": len(text), "status": "failed"}
            report["calls"].append(call)
            start = time.perf_counter()
            try:
                features = parser.featurize(text)
            except Exception as exc:
                call["parser_s"] = time.perf_counter() - start
                call["error"] = repr(exc)
            else:
                call["parser_s"] = time.perf_counter() - start
                try:
                    checked_features(features)
                    if len(features) != 94:
                        raise ValueError("Expected exactly 94 features")
                    if call["parser_s"] > report["call_cap_s"]:
                        raise ValueError("Parser exceeded 15-second cap")
                    call.update(status="ok", feature_count=len(features),
                                cut_statistics={k: v for k, v in features.items()
                                                if k.startswith("sample_cut_")})
                except Exception as exc:
                    call["error"] = repr(exc)
            if call["status"] != "ok":
                report["failures"].append(dict(call))
        save()
        print(json.dumps({"fixture": name, "calls_completed": len(report["calls"]),
                          "failures": len(report["failures"])}), flush=True)

    for name, text in regression_fixtures():
        measure(name, text, "synthetic")
    for name, path in packet["benchmark_circuits"].items():
        try:
            text = read_qasm(path)
        except Exception as exc:
            report["failures"].append({"stage": "read", "fixture": name, "error": repr(exc)})
            save()
            continue
        measure(name, text, "public_packet_benchmark")
    report["expected_calls"] = (len(before["fixtures"]) + len(packet["benchmark_circuits"])) * report["repeats"]
    report["completed_calls"] = len(report["calls"])
    report["max_parser_s"] = max(call["parser_s"] for call in report["calls"])
    if report["completed_calls"] != report["expected_calls"]:
        report["failures"].append({"stage": "accounting", "error": "Missing parser calls"})
    save()
    print(json.dumps({"completed_calls": report["completed_calls"],
                      "max_parser_s": report["max_parser_s"],
                      "failures": len(report["failures"])}), flush=True)
    return int(bool(report["failures"]))


if __name__ == "__main__":
    raise SystemExit(main())
