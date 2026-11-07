# Quantum Circuit Explorer

This is the canonical local Visualizer in `qasm-runtime-predictor/visualizer/Visualizer`.
Make all Visualizer changes here and launch this copy with `start.ps1`.
The separate `QuantumRings/Visualizer` directory is obsolete.
`/api/health` reports `application_root` so the running folder can be verified.

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
   **Fullscreen**, scroll vertically through qubits or horizontally through gates,
   pinch (or Ctrl+scroll) to zoom, **Fit circuit** for the full
   navigator, and **Focus selected** to return to the selected instruction.
3. **Inspect features here** extracts the current prefix's model inputs.
4. **Predict runtime here** predicts a fresh simulator run through that step,
   then loads its signed top-10 SHAP explanation separately.
5. **Calculate state here** calculates a small ideal-state checkpoint.
6. **qBraid connector** on the main dashboard includes an Upload QASM button and lets you select a configured account profile,
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

State calculations run manually for one selected qubit or a selected pair. The

backend walks backward through every multi-qubit dependency before dropping

irrelevant operations; selecting an output does not discard entangled inputs.

The reported causal cone can be smaller than the source circuit. Auto uses exact

Qiskit stabilizer evolution for supported Clifford cones (up to 512 qubits),

otherwise a floating-point statevector through 22 cone qubits, otherwise Aer MPS

through 128 cone qubits. The response and UI identify the actual method and accuracy.

MPS is always labeled approximate: the default maximum bond is 64 and truncation

cutoff is 1e-10, adjustable within 2–128 and 0–0.001. No certified error bound is

claimed. Compare larger bonds and smaller cutoffs for convergence; arbitrary wide,

highly entangled circuits are not guaranteed to fit these budgets.



Each result includes only the selected Bloch vectors, P0/P1, purity, entropy and,

for a pair, XX/YY/ZZ correlations and concurrence. Full joint outcomes, phases and

amplitudes are optional and restricted to 12 **total** circuit qubits. Only this

explicit small global calculation creates a complete ideal distribution for cloud

comparison; selected marginals never become a global reference. Its preview shows

at most 64 outcomes, while the complete reference contains at most 4096 entries.



There is one state worker, a 30-second wall budget including startup, and a 1 GiB

Windows Job Object memory budget with process-tree cleanup. Statevector storage is

16 × 2^n bytes before workspace, with an independent hard 22-qubit cone ceiling.

All methods retain a 10,000-expanded-operation and 64-level custom-gate nesting

budget. Source hash/checkpoint, selected qubits, requested method, joint option and

MPS settings identify the bounded result cache. Timeout, memory and unsupported

method failures produce errors instead of fabricated state metrics.

Measurement, reset and conditional branches are visible state boundaries.
Conditional `if` blocks can be uploaded and inspected as collapsed, unevaluated
source steps; runtime features use the unchanged source and upstream structural
heuristics. Large conditional programs use a bounded source view instead of a
full Qiskit import. Loops, standalone else, calibration, external includes, gate
modifiers, aliases, symbolic inputs and unsupported syntax produce explicit errors.
This is not a complete compiler; see [conditional upload details](BACKEND-MODEL.md#conditional-uploads).

Uploads accept up to **256 MiB** of exact, allowlisted UTF-8 QASM by default.
Set `VISUALIZER_MAX_UPLOAD_BYTES` to a positive integer through `268435456`
to lower the byte ceiling. `/api/health` and the upload UI report the active limit.
Requests stream to a private session file and are SHA-256-authorized before
UTF-8 decoding or any inspection. Unknown contents remain blocked, regardless of size.

Interactive inspection has separate limits: **32 MiB, 100,000 source or expanded
instructions, 2,048 qubits, and a 10-second native/preprocessing deadline**.
The app-owned native executable receives the same configured byte ceiling as
the API; standalone native inspection defaults to 256 MiB. Its JSON writer stops
at 64 MiB. Source/body previews are bounded to 4,096 characters; exact source and
offsets still determine prefixes and model inputs. The viewport pages instructions
and renders visible gates. Python SOURCE fallback is limited to 5 MiB.

Files up to 32 MiB with more than 100,000 top-level statements open in
**windowed native inspection** (`inspection_mode=windowed`). The C++ inspector
indexes every statement once, then inspects windows of 100,000 statements on
demand (at most four retained per circuit). Only source-step navigation is
available; features, prediction and SHAP use the exact source through the
selected statement. Stepper, state (12 qubits/10,000 operations) and window
record limits (100,000 expanded records) are unchanged, and cloud execution is
unavailable. See [windowed inspection](BACKEND-MODEL.md#windowed-native-inspection).

Files above 32 MiB, and files above 5 MiB whose interactive inspection fails,
open in **Full circuit mode**. The original file is retained on disk; it does not
enter Python statement parsing or Qiskit. Syntax and qubit/gate counts are
unvalidated in this mode. The only checkpoint is **Full circuit** (API source step
1); diagram, arbitrary prefixes, state, stepper and cloud execution are unavailable.
The exact, unnormalized source is passed to the unchanged pinned predictor in a
separate Windows process. Prediction and SHAP reuse the resulting feature cache.
This mode does not imply that millions of instructions can be visualized or
that every accepted file can finish feature extraction within its resource budget.

One full-file extraction may run at a time. Its default budget is **180 seconds
and 3 GiB aggregate committed memory**, including native descendants, enforced
by a Windows Job Object with process-tree cleanup. Configure
`VISUALIZER_LARGE_FEATURE_TIMEOUT` (1–600 seconds) and
`VISUALIZER_LARGE_FEATURE_MEMORY_BYTES` (256 MiB–8 GiB) if appropriate for the host.
Timeouts and extraction/memory failures produce explicit errors, never a partial
source prediction. Upstream parser failures that would substitute all-zero raw
features are surfaced as failures in this mode. Native feature fallback/truncation
remains labeled without changing the predictor's features.

State calculations use the independent method, causal-cone and operation budgets above.
Four circuit records, 64 feature/prediction results and two concurrent uploads
are bounded per session. File-backed records are removed on eviction and shutdown;
active extraction leases defer deletion. There are at most two parse/state workers.
Calculation wall time is separate from the target simulator runtime estimate.

## Model explanations

Local Tree SHAP explanations follow each explicit prediction and report **log10(seconds)** contributions to the
raw mean output of the four tree regressors before timeout/cap postprocessing. They are not additive seconds or causal effects.
Up to ten largest absolute contributions retain their signs and input values;
when fewer exist, all available contributions are shown. **Explain this
checkpoint** also requests SHAP independently of prediction. The selected step
and threshold determine which explanation appears; navigating or changing the
threshold never silently computes a new explanation. Derived inputs supplement
raw features, so seven derived inputs do not limit the SHAP chart to seven rows.
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

For local testing, enter an API key in the page's **Connect qBraid for local
testing** password field. The key is held in server memory for the connection,
cleared from the input on connection, and requires reconnection after restarting
the server. Environment-based profiles remain supported. See [QBRAID.md](QBRAID.md)
for reconnecting saved jobs and disconnect behavior. The
app reads actual wallet balances, shows estimates when available, and never
automatically switches keys on exhaustion or errors. No cloud job runs until the
user reviews and submits it. No audit evaluation or training is performed.

## Standalone 3D prototype

The separate `bloch-3d-demo` package contains a Blender scene, exported GLB,
Qiskit fixtures, an interactive Three.js gallery and a GNU Makefile. It is a
portable prototype for review on another machine; the main app keeps its existing
Qiskit state panel. Camera rotation does not change quantum state. The demo's own
README documents `make demo`, optional fixture/asset regeneration and requirements.


Selected-state implementation references: [Aer simulator methods and memory/MPS

options](https://qiskit.github.io/qiskit-aer/stubs/qiskit_aer.AerSimulator.html),

[Aer selected density-matrix saves](https://qiskit.github.io/qiskit-aer/stubs/qiskit_aer.library.save_density_matrix.html),

and [Qiskit observable light cones](https://quantum.cloud.ibm.com/docs/en/api/qiskit/qiskit.transpiler.passes.LightCone).

The app uses a conservative whole-subsystem backward support cone rather than a

single-Pauli observable cone. The simulator dependency is pinned to Aer 0.18.2;

the deployed integration was checked with Qiskit 2.6.2.

