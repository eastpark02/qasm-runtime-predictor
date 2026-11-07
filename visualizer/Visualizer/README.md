# Quantum Circuit Explorer

A local React/TypeScript + FastAPI workbench for stepping through QASM, manually
predicting simulator runtime, and calculating small ideal quantum states.

## Start on Windows

Prerequisites: Python **3.12** and Node **20.19+ or 22.12+**. `uv` is supported but
not required. From this folder in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

Setup creates `.venv`, installs pinned Python dependencies and the frontend lock,
builds the frontend, native runtime stepper and native circuit inspector, and preserves the existing development
allowlist. It never opens the circuit corpus or audit labels or approves new inputs.
Use `setup.ps1 -BackendOnly` to install only backend dependencies/runtime without
touching frontend files. See [the backend model guide](BACKEND-MODEL.md).
Use `setup.ps1 -Python 'C:\full\path\python.exe'` if necessary, or
`-SkipDevelopmentAllowlist` for compatibility (setup always preserves the allowlist).

The launcher serves everything at <http://127.0.0.1:8000>, opens the browser after
health is ready, and stays in the terminal. Stop with Ctrl+C. `-NoBrowser` skips
opening a browser. It does not expose the app to the network.

## Use

1. Open a synthetic example or upload an **exact, allowlisted UTF-8 `.qasm` file**.
   Download buttons provide the exact example files for testing uploads.
2. Click a gate, use previous/next, drag the slider, or enter a step number. Use
   **Fullscreen**, drag to pan, wheel/pinch to zoom, **Fit circuit** for the full
   navigator, and **Focus selected** to return to the selected instruction.
3. **Inspect features here** extracts the current prefix's model inputs.
4. **Predict runtime here** predicts a fresh simulator run through that step,
   then loads its signed top-10 SHAP explanation separately.
5. **Calculate state here** calculates a small ideal-state checkpoint.
6. **Run this checkpoint on qBraid** lets you select a configured account profile,
   simulator/QPU and shots, review an estimate, then explicitly submit one job.
   See [qBraid setup and billing behavior](QBRAID.md).

Navigation never predicts or simulates. Results remain labeled with their actual
checkpoint when you move elsewhere; calculate again to update. Threshold changes
select a different prediction history. Pair selection changes the state cache key.
Browser refresh clears UI history; server caches are bounded and in memory only.

## Audit exclusion

`approved-development.json` is a hash-only allowlist, derived from the existing
`ML-Agent/runs/iqc-2026-runtime/models/cycle-0001/fit_development-input.json`.
The preparation script accepts only `operation=fit_development`, development
folds 0–4, and valid content SHA-256 hashes. It never traverses a corpus. Target
labels are not copied into the app. Bundled synthetic examples are also approved.

Every upload is hashed **before Qiskit parsing or model inference**. Filenames
cannot approve a file. Unknown or modified files are rejected with 403. The server
also rechecks authorization before computations. There is deliberately no browser
button or API to approve unknown files. Do not add hashes until the input has
explicitly been confirmed non-audit. Do not add audit data to this application.
Only decompressed `.qasm` uploads are accepted; upload limits apply to raw bytes.

## Runtime checkpoint

Default provider: the pinned `qasm-runtime-predictor` **dev** commit
`4de52d4365ed8d966f662a75a7431b9dafb2a0af`, using a four-member
HistGradientBoosting blend. Checkpoint SHA-256:
`becf030147191a76bb74b3fd4384b89f296c1fb344148828082f48c1dca517e5`.
Upstream reports fitting **2,648 rows / 532 circuits** (`upstream_all_rows`);
this is not the earlier development-only checkpoint. No raw-label twin index
is installed. Upstream CV scores are not clean audit evidence.

`runtime-checkpoint.json` pins all inference source bytes. Each server session
loads an isolated verified snapshot and reports artifact/source/binary fingerprints
at `/api/health`; newer checkout commits are never silently adopted. The unchanged
upstream adapter performs extraction, prediction and timeout/cap postprocessing.
See [BACKEND-MODEL.md](BACKEND-MODEL.md) for backend setup, stepper contract,
limits, diagnostics, fingerprints and explanation semantics. `VISUALIZER_MODEL=legacy`
explicitly selects the older development-only provider; `VISUALIZER_PREDICTOR`
overrides the dev checkout location while retaining exact hash checks.

The predictor checkout and its artifacts, scorer, split and training controls
are never modified. A missing or mismatched model leaves circuit/state inspection
available and shows a diagnostic. There is no training or dataset-evaluation job.

The app-owned C++ inspector uses the pinned dev parser source to expand supported
custom gates and broadcasts into selectable primitives. Each primitive retains
its source location and custom call path. Unsupported inspection falls back to
the existing Python source view with a visible reason; it does not hide that
change behind a separate stepper panel. Inspection and runtime feature extraction
have separate provenance: predictions always run the Python four-model blend,
while structural features report the actual C++ or Python extraction status.

Prefixes preserve declaration/definition spans, source order and executed source
text at source boundaries. A checkpoint inside an expanded call generates the
executed primitive prefix and is visibly labeled. A full-circuit prediction uses
the exact original text. Prefix predictions remain experimental because training on complete
circuits does not validate shorter prefixes. Differences between predictions are
**model-estimated changes**, not measured or causal gate costs. Threshold must
remain fixed for a comparison. Source-size/sampling features can differ between
prefixes, including because retained custom definitions may contain unused gates.

## Quantum-state meaning and limits

Qiskit calculations start in the all-zero state, without noise. Per-qubit Bloch
vectors, computational-basis probabilities and reduced-state entropy are exact
within numerical precision. Entropy is in bits, from 0 to 1, and measures a qubit
versus the rest **only under this pure-global-state assumption**. Do not interpret
it as an entanglement measure for arbitrary noisy mixed global states.

Selected-pair concurrence is 0–1. Zero does not rule out multipartite entanglement
(see GHZ). XX/YY/ZZ correlations and joint amplitude phases show information that
individual Bloch spheres can hide. A Bell pair has both local vectors at the
center; local RY/RZ gates can change the joint state without changing that fact.
Bloch arrows are coordinates, not physical trajectories or entanglement directions.
Bitstrings use Qiskit's convention: **q[n−1]…q[0]**.

State calculations allow 12 qubits and 10,000 expanded operations. Custom gate
bodies are recursively checked before evolving the state, with a 64-level nesting
limit. Calculation workers are killed after 10 seconds (including startup).
Measurement, reset and conditional branches are visible state boundaries.
Conditional `if` blocks can be uploaded and inspected as collapsed, unevaluated
source steps; runtime features use the unchanged source and upstream structural
heuristics. Large conditional programs use a bounded source view instead of a
full Qiskit import. Loops, standalone else, calibration, external includes, gate
modifiers, aliases, symbolic inputs and unsupported syntax produce explicit errors.
This is not a complete compiler; see [conditional upload details](BACKEND-MODEL.md#conditional-uploads).

Inspection is capped at 5 MiB UTF-8 input and 2,048 qubits, with a killable
10-second import worker. Native expansion is capped at 100,000 records; unsupported
or over-limit expansion uses the labeled source view when that view can accept
the input. The circuit viewport renders only visible wires/gates, pages instruction
data in bounded batches and provides a whole-circuit overview. Custom calls and
broadcasts stay collapsed only in source fallback. Four circuits and 64 computed
results are cached. There are at
most two simultaneous parse/state workers. Quantum calculation wall time is
reported separately from the model's target-simulator runtime estimate.

## Model explanations

Local Tree SHAP explanations follow each explicit prediction and report **log10(seconds)** contributions to the
raw mean output of the four tree regressors before timeout/cap postprocessing. They are not additive seconds or causal effects.
The ten largest absolute contributions retain their signs and input values.
Baseline, raw output, the other contributions' sum and any final timeout/cap
adjustment are shown. All raw features and threshold-derived inputs remain
searchable in separate tabs. An explanation failure does not discard a prediction;
retry stays attached to the selected checkpoint.

Global permutation importance is unavailable unless a separately approved report
is installed as `approved-importance.json`. No training impurity rankings are
substituted. The server verifies `scope=development_only`, `method=permutation` and
an exact `model_fingerprint` match. Reports must contain `metric` (including its
target scale) and `rows: [{feature, importance}]`. Only import reports derived from
approved development validation; the app does not run evaluation. R² describes
overall regression performance, not the influence of an individual feature.

## Development and checks

```powershell
.\.venv\Scripts\python.exe -m pytest -q
cd frontend
npm.cmd run build
npx.cmd playwright install chromium
npm.cmd run test:e2e
```

Browser tests start their own loopback server on port 8000; stop the normal app
first, or set `VISUALIZER_TEST_URL` to an already-running local test server. They
use synthetic circuits and mocked qBraid services. API docs: `/docs`.
For frontend live editing, run the backend with the launcher and `npm.cmd run dev`
in `frontend`; Vite proxies `/api` to port 8000.

The qBraid SDK is configured server-side with explicitly selected profiles. The
app reads actual wallet balances, shows estimates when available, and never
automatically switches keys on exhaustion or errors. No cloud job runs until the
user reviews and submits it. No audit evaluation or training is performed.

## Standalone 3D prototype

The separate `bloch-3d-demo` package contains a Blender scene, exported GLB,
Qiskit fixtures, an interactive Three.js gallery and a GNU Makefile. It is a
portable prototype for review on another machine; the main app keeps its existing
Qiskit state panel. Camera rotation does not change quantum state. The demo's own
README documents `make demo`, optional fixture/asset regeneration and requirements.
