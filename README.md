# QASM Runtime Predictor

Predict execution time for OpenQASM 2.0 and 3.0 circuits from their source
structure. The project combines a streaming parser, an MPS-inspired gate
stepper, structural circuit fingerprints, and an ensemble of gradient-boosted
regressors.

The shipped model predicts runtime for a circuit/threshold pair in seconds.
It is designed for the Quantum Rings IQC 2026 runtime-prediction challenge,
but the parser and feature pipeline are useful independently.

## Highlights

- **OpenQASM 2.0 and 3.0 parsing** without constructing a Qiskit circuit object.
- **Streaming feature extraction** for registers, gates, depth, interactions,
  conditional operations, measurements, resets, custom gates, and parameters.
- **Custom-gate support**, including nested calls and register broadcasting.
- **C++20 moment stepper** that expands custom gates and estimates
  threshold-dependent MPS bond-dimension cost gate by gate.
- **Pure-Python fallback** when the native stepper is unavailable, fails, or
  reaches its deadline.
- **Structure fingerprints** for QFT, adders, Sycamore-style layers, GHZ
  chains, Grover-like repetition, Bernstein-Vazirani oracles, arithmetic
  multi-controlled gates, layer periods, and hashed gate motifs.
- **Compressed input support** for `.qasm.zst` files during dataset feature
  generation.
- **Cross-validated model training** with circuit-grouped folds, timeout-aware
  scoring, threshold-derived features, and a four-member log-space blend.
- **Harness-ready submission bundle** containing the model, feature code, and
  native stepper.

## Results

The checked-in artifact reports five-fold `GroupKFold` cross-validation by
circuit filename:

| Model | Official CV score |
| --- | ---: |
| Median runtime per threshold | 0.2841 |
| Base feature model | 0.8193 |
| Shipped `blend4` | **0.8401** |

The report covers 2,648 rows from 532 circuits. Scores use the challenge
metric, `max(0, 1 - abs(log10(predicted / actual)))`, with timeout labels
represented by the six-hour cap of 21,600 seconds. See
[`artifacts/cv_report.json`](artifacts/cv_report.json) for the complete
breakdown by threshold and QASM version, and
[`docs/EXPERIMENTS.md`](docs/EXPERIMENTS.md) for ablations and known limits.

## Repository layout

| Path | Purpose |
| --- | --- |
| [`qasm_features.py`](qasm_features.py) | Streaming OpenQASM feature extractor and CLI |
| [`qasm_step.py`](qasm_step.py) | Native-stepper wrapper, timeout handling, and fallback selection |
| [`qasm_step_py.py`](qasm_step_py.py) | Pure-Python stepper with the native feature contract |
| [`build_features.py`](build_features.py) | Parallel `.qasm`/`.qasm.zst` feature generation |
| [`train.py`](train.py) | Training, cross-validation, blending, and artifact export |
| [`submission/model.py`](submission/model.py) | Harness entry point: featurize and predict |
| [`stepper/qasm_step.cpp`](stepper/qasm_step.cpp) | Portable C++20 parser and MPS-style moment stepper |
| [`stepper/`](stepper/) | Native build scripts, CMake project, token survey, and stepper reference |
| [`features.csv`](features.csv) | Checked-in example feature matrix |
| [`artifacts/model.pkl`](artifacts/model.pkl) | Shipped serialized `blend4` model |
| [`artifacts/cv_report.json`](artifacts/cv_report.json) | Metrics and model metadata for the shipped artifact |
| [`docs/EXPERIMENTS.md`](docs/EXPERIMENTS.md) | Detailed experiment log and limitations |
| [`docs/ONE_PAGER.md`](docs/ONE_PAGER.md) | Concise technical summary |
| [`install_into_harness.sh`](install_into_harness.sh) | Copies the submission bundle into the challenge harness |

## Requirements

- Python 3.9 or newer recommended
- A C++20 compiler and CMake 3.16 or newer for the native stepper
- Python packages listed in [`requirements.txt`](requirements.txt)
- `qiskit` is optional and is only needed for parser validation experiments

Install the Python dependencies:

```bash
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows PowerShell:
# .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Build the native stepper

The wrapper searches for `qasm_step`/`qasm_step.exe` in `stepper/`,
`stepper/build/Release/`, `stepper/build/`, and the project root.

### Windows PowerShell

With Visual Studio Build Tools and CMake on `PATH`:

```powershell
PowerShell -ExecutionPolicy Bypass -File .\stepper\build.ps1
```

With MinGW:

```powershell
$env:CMAKE_GENERATOR = "MinGW Makefiles"
PowerShell -ExecutionPolicy Bypass -File .\stepper\build.ps1
```

If switching CMake generators, remove `stepper\build\` first. The script
copies the resulting executable to `stepper\qasm_step.exe`.

### Linux/macOS/Git Bash

```bash
./stepper/build.sh
```

The build scripts use the same release CMake configuration and copy the
resulting binary next to the source files.

## Use the parser and stepper

Extract static features from one or more circuits:

```bash
python qasm_features.py path/to/circuit.qasm
```

Run the native stepper:

```bash
stepper/qasm_step --thresholds 16,64,256,512,1024 path/to/circuit.qasm
stepper/qasm_step --trace path/to/circuit.qasm
```

The final native-stepper output line is a flat JSON object containing numeric
`st_*` fields. `--trace` additionally prints one line per expanded primitive
gate, which is useful for validating bond-dimension updates and costs.

Python API examples:

```python
from qasm_features import featurize
from qasm_step import step_features

text = open("circuit.qasm", encoding="utf-8").read()
static = featurize(text)
stepper = step_features(text, deadline_s=12.0)
print(static["n_qubits"], static["n_gates"])
print(stepper["st_log10_cost_1024"])
```

`qasm_step.step_features()` never raises for normal operational failures. It
uses the native result when valid and otherwise returns the Python fallback.
The fallback marks its status in `st_ok`: `1.0` native success, `0.75`
completed fallback, `0.5` deadline-limited fallback, `0.25` no-gate deadline
guess, and `0.0` fallback failure.

## Generate a feature matrix

The generator reads `.qasm` and `.qasm.zst` files from one directory, processes
them in parallel, and writes one CSV row per circuit:

```bash
python build_features.py \
  --circuits ../quantumrings-challenge-iqc-2026/raw-circuits \
  --out features.csv \
  --workers 8 \
  --fallback-frac 0.03
```

The fallback fraction is deterministic by filename hash, which makes it
possible to exercise both implementations consistently. Native stepping starts
before static parsing when the input is an uncompressed file so the work can
overlap.

The feature matrix contains:

- Static parser features such as `n_gates`, `n_2q`, depth, interaction
  distance, cut-crossing counts, entanglement capacity, and log transforms.
- Stepper features such as `st_log10_cost_<threshold>`, endpoint costs,
  saturation, realized entangling updates, basis-state fractions, and
  fingerprints.
- A 64-bin hashed 3-gram motif vector (`st_motif_0` through `st_motif_63`)
  plus motif entropy and concentration.

## Train and evaluate

Training labels must contain `filename`, `threshold`, `duration_s`, and
`status` columns. Timeout rows are converted to `log10(21600)` targets.

Train the shipped blended model:

```bash
python train.py \
  --features features.csv \
  --labels ../quantumrings-challenge-iqc-2026/train-runtime-data.csv \
  --out artifacts \
  --blend \
  --folds 5 \
  --jobs 8
```

The training code groups all thresholds for a filename into the same fold,
preventing circuit leakage. The default blend averages four HGB regressors in
log10 space:

1. Static/base features without monotonic constraints.
2. Static plus stepper features.
3. Stepper features with a selected threshold cost and monotonic constraint.
4. Stepper features with cost-normalized features.

Structural-twin features are implemented for investigation but are disabled
for the shipped artifact because they reduced cross-validation performance.
Use `--no-twin` to make that choice explicit. The non-blended baseline can be
trained with the script's single-model options; run `python train.py --help`
for the complete CLI.

## Run the submission model

`submission/model.py` exposes `RuntimeModel`, which loads
`artifacts/model.pkl`, validates the native stepper with a smoke circuit, and
provides:

```python
from submission.model import RuntimeModel

model = RuntimeModel()
features = model.featurize(qasm_text)
seconds = model.predict(features, threshold=1024)
```

Predictions are capped at 21,600 seconds. If any blend member predicts at
least 60% of the cap, the model predicts the timeout cap. Native stepper
failures are reported to stderr and use the Python fallback where possible;
model loading fails early if the required native binary is missing or broken.

To install the complete bundle into the challenge harness:

```bash
./install_into_harness.sh ../quantumrings-challenge-iqc-2026/submission-harness
```

The script copies `model.py`, both parsers, the trained artifact, and the
available native binary. Build the binary first; otherwise installation
completes with a warning and the harness model will reject startup.

## Validate a holdout circuit set

Before a holdout run, survey statement and parameter tokens:

```bash
python stepper/survey_tokens.py ../holdout-circuits
```

The expected success message is `OK: no unknown tokens`. The survey is a
coverage gate, not a correctness proof: it checks observed identifiers against
the maintained known-token list and recognizes custom gates defined in each
file.

## Compatibility and limitations

- The native parser is portable C++20 and supports OpenQASM 2/3 constructs
  used by the challenge corpus, including custom gates, modifiers, conditionals,
  and parameter expressions.
- Unsupported statement blocks are skipped and counted in
  `st_n_skipped_stmts`; unknown gates are counted in
  `st_n_unknown_gates`.
- Native expansion is capped at 200 million primitive gates and custom-gate
  recursion at depth 64.
- The Python fallback intentionally emits zero for native-only family and motif
  fingerprints and may scale or guess cost features when its deadline expires.
- The stepper models declared qubit order and long-range gates as cut updates;
  it is a runtime proxy, not a physical quantum simulator.
- Short, heavily entangled circuits and timeout rows remain difficult cases.
  See [`docs/ONE_PAGER.md`](docs/ONE_PAGER.md) for the measured error profile.

## License

This project is released under the [MIT License](LICENSE).
