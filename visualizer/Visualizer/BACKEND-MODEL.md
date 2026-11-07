# Local dev runtime model

The default provider is the pinned `qasm-runtime-predictor` **dev** revision
`4de52d4365ed8d966f662a75a7431b9dafb2a0af`. Model SHA-256:
`becf030147191a76bb74b3fd4384b89f296c1fb344148828082f48c1dca517e5`.
The four HistGradientBoosting regressors are averaged in log10(seconds), then
the upstream timeout rule and six-hour cap are applied by the unchanged
`submission/model.py` adapter. There is no structural-twin raw-label index.

Upstream reports fitting **2,648 rows / 532 circuits**. This checkpoint has
`training_scope=upstream_all_rows`; it is not the earlier development-only model.
Upstream cross-validation scores are not clean audit evidence. The visualizer's
input policy remains development/synthetic only. Installation and tests never
open audit circuits or labels, scan the corpus, train, or evaluate a dataset.

## Install or start the backend

Use this application's `.venv/Scripts/python.exe` (Python 3.12). On Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -BackendOnly
powershell -ExecutionPolicy Bypass -File .\start.ps1 -NoBrowser
.\.venv\Scripts\python.exe -m pytest -q
```

`-BackendOnly` installs Python dependencies and prepares the native runtime,
then exits before touching the frontend. Setup preserves the existing allowlist;
it never approves new inputs. `-SkipDevelopmentAllowlist` remains accepted for
launcher compatibility. The existing frontend build is served by `start.ps1`.
For an API-only launch without requiring a frontend build:

```powershell
$env:PYTHONPATH = Join-Path $PWD 'backend'
$env:VISUALIZER_MODEL = 'dev'
.\.venv\Scripts\python.exe -m uvicorn visualizer.app:app --host 127.0.0.1 --port 8000
```

`VISUALIZER_PREDICTOR` overrides the checkout location, but all six inference
files must still match `runtime-checkpoint.json`. `VISUALIZER_MODEL=legacy`
explicitly selects the older development-only provider. Neither provider is
hot-reloaded. Restart deliberately after reviewing any manifest change.

The portable compiler is in `.local/toolchains`. `scripts/build-stepper.ps1`
checks the pinned upstream C++ source before compiling and records source and
executable SHA-256 hashes in `.local/runtime/build.json`. It reuses an existing
verified build; pass `-Force` to rebuild. No Visual Studio C++ workload is needed.
New metadata is BOM-free UTF-8; the loader also accepts PowerShell 5 UTF-8 BOMs.

`scripts/build-inspector.ps1` separately builds the app-owned `qasm_inspect.exe`.
Its strict parser reuses expressions from a byte-identical copy of the pinned
dev C++ source, with an independent grammar and executable fingerprint. It never
replaces `qasm_step.exe`, changes `st_*` extraction, or runs inference. Source and
executable fingerprints are checked before use. The server freezes its initial
inspector fingerprint; changed or newly installed builds require a restart.

## Native inspection and exact checkpoints

Upload first attempts native expansion. Supported custom calls expand recursively
in execution and broadcast order, retaining every quantum/classical operand,
evaluated parameter, original statement origin, and custom-call path. Builtins
remain leaves. `inspection_mode=native_expanded` and
`inspection_backend=app_owned_cpp` identify this view. Limits are 5 MiB, 2,048
qubits, 100,000 expanded records, 64 levels of nesting, a ten-second native
subprocess timeout and two concurrent upload/state/trace calculations.

Conditionals, unsupported native syntax, missing/changed builds, native failures
and expansion overflow attempt the existing Python SOURCE view in a separate
ten-second killable worker. `fallback_reason` records the observed inspection
failure. Invalid source still fails explicitly. SOURCE view never labels source
records as expanded primitives. Native acceptance is deliberately conservative:
no modifiers, nonunitary custom bodies, forward gate references, declarations
after executable statements or unknown gate signatures.

`GET /api/circuits/{id}/steps` retains source-statement navigation. The additive
`/instructions?offset=0&limit=80` returns `{offset,total,steps,step_kind,provenance}`
with a maximum page size of 200. `/overview?buckets=100` returns at most 200
compact `{start,end,count}` buckets. Summary adds `total_instructions`,
`expanded_state_boundary`, `inspector_fingerprint` and inspection provenance.

All computational checkpoint requests accept `step_kind: "source" | "expanded"`,
defaulting to `source`. Step N means N applied records, and 0 is the initial
checkpoint. Expanded selections require successful native inspection. Full
selections use the exact original input; completed source boundaries use the
existing source-prefix builder. An interior custom-call or broadcast checkpoint
preserves completed source statements and replaces only the active statement
with applied primitive instructions, using roundtrip numeric parameters. Features,
prediction, state, trace and explanation receive that same prefix. Exact state
retains its 12-qubit/10,000-operation limit and stops before measurement, reset
and classical branches.

Results carry `step_kind`, `input_hash`, `prefix_hash`, `prefix_kind` and
`source_origin`. Prefix kinds are `original_full_source`, `source_boundary` and
`generated_primitives`. Computational caches use the prefix hash, model
fingerprint, and applicable threshold/state pair. Authorization is rechecked on
cache hits. Equivalent source/expanded boundaries can share results while
retaining the requested checkpoint metadata.

Inspection provenance is distinct from structural feature provenance. Upstream
`st_ok=1` identifies native C++ structural extraction; `.75` identifies complete
Python fallback; `.5`/`.25` identify partial Python fallback. `st_truncated` is
exposed unchanged. The adapter discards the reason for structural fallback, so
that reason stays unknown. Prediction always remains the pinned Python
four-HistGBDT blend regardless of inspection or structural extraction path.

`/features` and `/predict` preserve `features` and also return `raw_features`,
selected `threshold`, upstream `derived_features`, and exact per-member
`model_inputs` including upstream missing-column zero defaults. These are views
of unchanged model inputs, never corrected or normalized substitutes.

Each server session copies the exact verified artifact, adapter, extractors,
native source and executable into a unique `.local/sessions/` snapshot.
Extraction and tracing use that executable, and reject changes to its bytes.
Loaded trees and Python modules stay fixed in memory. Health exposes the commit,
artifact fingerprint, every pinned source fingerprint, executable fingerprint,
snapshot path, provenance, supported thresholds and capability flags. Changed
upstream bytes fail closed instead of adopting a newer dev commit.

## Manual API contract

Navigation, upload and opening examples do not predict, step or calculate a
quantum state. Upload authorization happens before parsing; each computational
entry point checks authorization again, including cached results.

`POST /api/circuits/{id}/stepper` takes `{ "step": 1, "threshold": 256 }`.
Supported runtime/trace/explanation thresholds are **16, 64, 256, 512, 1024**;
other values return 422 before extraction. Source step 0 means declarations only.
The response preserves:

```text
{step, threshold, cached, calculation_seconds, expanded_gates, features, gates, note}
gate = {index, name, qubits, bond_before, bond_after, added_cost, total_cost}
```

`qft_blocks` is an additional upstream detector diagnostic. Native gate indices
start at **0**. Custom gates expand recursively in execution order; expanded
indices are separate from source steps. A trace is limited to **10,000 expanded
gates**, **4 MiB output** and a **10-second native process timeout**. Timed-out
processes are terminated. Incomplete/fallback feature passes cannot be traced.
Trace gate counts and selected-threshold cost must agree with the original
adapter feature pass. Trace cache keys include input/prefix hashes, model fingerprint and
threshold; four traces are retained. Error status is 422 for unsupported inputs,
408 for timeout, 429 when the two-worker limit is occupied, and 403 if input
authorization is absent or revoked.

Native bonds and costs are **structural workload heuristics**, not Qiskit state
or entanglement measurements. `bond_before`/`bond_after` are log2 bond-dimension
heuristics at the first affected cut, with zero for one-qubit gates. Thresholds
cap cost calculations, not the reported bond exponent. The native trace reports
only the first two operands of gates with three or more operands; the gate name
and `st_n_3q_plus` retain that limitation explicitly in the note.

Feature dictionaries preserve the upstream extractor exactly. `/features` and
`/predict` add `diagnostics: string[]`; the stepper appends relevant diagnostics
to `note`. Differences between source/imported instructions, expanded gates,
measurement/reset counts, Python extractor counts and native counts are reported
without adjusting model inputs. Custom calls and non-gate instructions can
explain count differences. Parser skip/error counters and fallback/truncation
are also reported. The trace's `features` are its native summary at the requested
threshold; use `/features` for the complete model input dictionary, which includes
all five threshold features. Unsupported frontend/Qiskit source syntax fails
explicitly rather than creating a different circuit.

## Manual explanations

`POST /api/circuits/{id}/explain` uses the same `{step, threshold}` selection.
Mean Tree SHAP contributions over all four members explain the **raw mean
log10(seconds) before timeout/cap postprocessing**. Every explanation checks
`base_value + contribution_sum == output` within numerical tolerance. The top
10 entries alone need not sum to the output; `contribution_sum` includes every
feature. `predicted_seconds` is the final upstream prediction, and
`postprocessing_applied` identifies when its log differs from the raw output.
Signed contributions are ranked by absolute SHAP magnitude. The response also
includes `raw_output`, `raw_predicted_seconds`, total contribution count,
model fingerprint and upstream timeout/cap metadata.
Contributions are not additive seconds, causal effects or measured gate costs.

If Tree SHAP is unsupported, health reports `explanation_available=false` with
an error, and the manual endpoint returns an explicit 422 capability error.
Global importance remains unavailable without a separately approved matching
development-validation report. No evaluation is run to produce one.


## Conditional uploads

Measurement-conditioned `if` statements are accepted through a bounded source
view (`inspection_mode=conditional_source`). Each block is one collapsed source
step; nested conditions and empty bodies remain intact. Full-circuit runtime
features and predictions receive the exact original text. Prefixes preserve
whole blocks rather than flattening or unconditionally executing their contents.

This source view avoids importing thousands of branches into Qiskit during
upload, retaining the ten-second parser-worker timeout. It is source inspection,
not a complete QASM compiler or a simulation of branch choices. Summary warnings,
feature/prediction diagnostics and native trace notes expose this distinction.
The model's conditional structural features are reused without modification.
Exact state calculation stops before the first measurement/reset/branch and
retains the twelve-qubit limit. Loops, standalone else, modifiers, calibration,
external includes and unsupported declarations remain explicit errors. Upload
hash authorization is still required before any parsing and every computation.
