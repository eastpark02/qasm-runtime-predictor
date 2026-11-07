# Quantum circuit runtime pipeline

This package predicts simulation duration from OpenQASM circuit content and the
simulator threshold. It does not simulate quantum circuits. Its models use
numeric structure features and log-runtime regression; filenames are join keys
only and never become model inputs.

The companion `ML-Agent` controller owns the frozen grouped validation protocol,
research loop, independent reviews, model promotion, and stopping rule. Use its
evaluation reports for model comparisons. The local `train` command fits all
supplied labels and does **not** produce an unbiased validation score.

## Run locally

Install with Python 3.12 using `python -m pip install -e .`. From this project:

```powershell
python -m unittest discover -s tests -v
python -m runtime_pipeline features --qasm path\circuit.qasm.zst
python -m runtime_pipeline train --circuits path\train-circuits --labels path\train-runtime-data.csv
python -m runtime_pipeline predict --qasm path\circuit.qasm.zst --threshold 256
python -m runtime_pipeline benchmark --circuits path\raw-circuits --largest 3
```

`model_config.json` controls the estimator. Supported initial kinds are `median`,
`extra_trees`, and `hist_gradient_boosting`. The agent loop can propose additional
implementations, subject to its reviewer and evaluator gates. Learned feature
vocabulary and imputation are fit separately inside each training call.

## Harness interface

The root `model.py` exports `RuntimeModel`. The unchanged official harness calls
`RuntimeModel()`, `featurize(qasm_text)`, and `predict(features, threshold)`.
The default trained file is `artifacts/model.joblib`; pass an explicit artifacts
directory to use another reviewed checkpoint. Only load local trusted artifacts.
Predictions are finite seconds bounded to `[1e-9, 21600]`.

## Feature definitions and limitations

The parser handles the supplied QASM 2/3 declarations, indexed and register-wide
operands, comments, parameterized gates, and custom gate definitions. It counts
gate histograms by recursively composing called definitions, without constructing
a decomposed circuit. Unused definitions do not contribute executed gate counts.

Dependency depth and graph measures are deliberately named **proxies**. Custom
gate durations use an operation-count/arity proxy; hyperedges use a chain
projection. For large programs, dependency analysis uses bounded text windows and
operation counts, recorded by `dependency_sample_fraction`. Programs above 8 MB
use bounded, evenly spaced text windows for gate histograms as well; estimates
are scaled by the full program's statement count. They expose
`gate_histogram_sample_fraction` and `expanded_counts_complete=0`, and do not
claim exact expansion of hundreds of thousands of gate definitions. Smaller
programs scan complete bodies and compose called definitions exactly. Counts are
static: gates in both sides of a conditional are included, without predicting
measurement outcomes. Conditional and loop counts are separate features.
Register-wide count correction is exact only when full
dependency analysis is available. Clifford counts are lower bounds because
rotation parameter values are not symbolically classified.

This is a structural feature parser, not a complete QASM validator/interpreter:
arbitrary classical control flow, aliases, symbolic register sizes, and gate
modifiers are not expanded. Unresolved dependency operands are reported as a
feature; malformed headers, register indices, and recursive gate definitions are
rejected. Included files are never executed or fetched. No cache of circuit
identities or source algorithms is part of inference.

Compressed reading uses zstandard streaming decompression, matching the official
harness's frame requirements. Keep raw and compressed copies in separate folders
to avoid duplicate circuit records.
