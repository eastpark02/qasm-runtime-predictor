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
The model clips all training targets and all predictions to `[1e-9, 21600]`
seconds; tree regressors fit `log10` of the clipped targets. This global model
policy is separate from official timeout scoring. The official scorer uses
21,600 seconds as the actual duration on timeout rows and caps predictions at
21,600 only on those rows. Successful rows use their measured duration. It floors
predictions at `1e-9`, scores `max(0, 1 - abs(log10(pred / actual)))`, and gives
missing predictions zero credit. The candidate's global clipping does not alter
those scorer rules.

## Feature definitions and limitations

The parser recognizes common QASM 2/3 declarations, indexed and register-wide
operands, comments, parameterized gates, and custom gate definitions. Its
histogram branch depends on the number of characters after comment replacement,
not the file's bytes or compressed size. Each matched comment becomes one space.
At **at most 8,000,000 comment-stripped characters**, it scans complete written
bodies and recursively composes recognized calls to custom definitions without
constructing a decomposed circuit. Unused definitions do not contribute to that
branch's gate histogram, though definition-summary features still include them.

At **more than 8,000,000 comment-stripped characters**, histograms sample the
written source, including unused gate-definition bodies and custom-call tokens.
Three windows near the beginning, middle, and end share a 1,500,000-character
budget, trimmed to semicolon boundaries. Counts are scaled by the ratio of full
source semicolons to sampled semicolons. These are written-body estimates: they
do not resolve custom-call multiplicities or certify executed-operation counts.
Unknown/custom names are pooled, never exposed as identity features. Main-text
features use the suffix after the last gate definition, so earlier main
statements can be omitted; register declarations are scanned across the whole
text. `gate_histogram_sample_fraction` records sampled text coverage.
`expanded_counts_complete` is the existing **size-branch indicator**: 1 for the
smaller branch and 0 for the larger branch. Its name does not certify executed
operations, control flow, resolved syntax, or complete broadcast multiplicities.

Counts are static. Recognized gates in both sides of a conditional are included;
measurement outcomes are not predicted. Loops are not executed or unrolled, and
written bodies are not multiplied by iteration counts. Conditional and loop
keywords also have separate count features. Gate parameters are not evaluated.

Dependency analysis uses at most 1,500,000 characters in the same three-window
scheme and stops at 25,000 recognized operation instances across all windows.
Each broadcast instance counts separately; a custom-call instance is not a full
primitive expansion. A broadcast exceeding the remaining budget contributes
only its processed prefix to depth, qubit usage, and graph features.
`dependency_sample_fraction` estimates the fraction of main-text characters
covered, weighting a partial statement by its processed broadcast fraction. It
includes text that is not a recognized operation and is not an operation-coverage
or syntax-completeness measure.

Register-wide histogram correction is applied only in the smaller branch when
dependency text coverage equals 1. Otherwise `n_ops`, `gate_*`, and derived
histograms retain uncorrected statement-based broadcast counts, which can
undercount operations. Even full coverage only corrects broadcasts recognized
by the parser. For example, a final `cx a,b;` on two 25,001-qubit registers reaches
the 25,000-instance budget, leaving `gate_cx=1` and
`expanded_counts_complete=1` despite incomplete broadcast coverage.

Depth and graph measures are proxies. Custom gates use an operation-count/arity
depth cost; multi-operand interactions use a chain projection. Sample windows
have independent dependency state; their maximum depth is divided by the text
coverage fraction. Graph statistics describe sampled interactions and include
isolated declared qubits. These are neither exact circuit depth nor a full
executed interaction graph. `clifford_fraction_lower_bound` and
`nonclifford_count_upper_bound` retain historical names but classify recognized
gate names. Rotation angles are not symbolically classified. Sampling, unused
bodies, static branches, and incomplete broadcast correction mean these values
are not rigorous lower/upper bounds for the executed circuit.

Four cut features summarize static sampled call spans in the parser's existing
register-offset order. `sample_cut_multiqubit_instances` counts processed,
resolved, non-measurement instances with at least two distinct operand indices.
Each instance adds one crossing to every cut from its smallest index up to,
but excluding, its largest. `sample_cut_crossings_max`,
`sample_cut_crossings_mean`, and `sample_cut_crossings_std` report the maximum,
mean, and population standard deviation across all `n_qubits - 1` cuts,
including cuts with zero crossings. All four are zero if no eligible instances
exist; a singleton circuit has no cuts.

Repeated calls count separately. Custom, unknown, and higher-arity calls each
contribute one opaque span per processed broadcast instance, without body
expansion or primitive-count weighting. Unresolved operands, measurements, and
instances with fewer than two distinct indices contribute no crossings.
The existing 1,500,000-character and 25,000-instance detail limits still apply;
measurements keep their existing operation-budget behavior. Sampled windows are
aggregated without coverage extrapolation or histogram scaling. Two interval
endpoint updates per event and one scan over declared cuts add O(A+n) work and
O(n) storage, where A is the processed operand count and n is declared qubits.
Consistent register renaming and operand-order reversal preserve these features;
arbitrary qubit permutations and declaration order may change them. They are
static sampled call-span counts, not entanglement measurements, circuit cutwidth,
executed-gate counts, or runtime bounds.

This is a structural feature parser, not a complete QASM validator/interpreter:
arbitrary classical control flow, aliases, symbolic register sizes, and gate
modifiers are not expanded. Unresolved dependency operands are reported as a
feature. Malformed headers, register indices encountered in dependency analysis,
and recursive definitions reached by small-branch expansion are rejected;
unexamined text is not fully validated. Included files are never executed or
fetched. No cache of circuit identities or source algorithms is part of inference.

Compressed reading uses zstandard streaming decompression, matching the official
harness's frame requirements. Keep raw and compressed copies in separate folders
to avoid duplicate circuit records.

## Candidate-local timing evidence

Run the dedicated measurement script with the controller's public development
packet and its supplied Python environment:

```powershell
python validation/measure_baseline.py --development-inputs path\development\inputs.json
```

It fits the unchanged 400-tree configuration with seeds 42 and 43 using only the
packet's fold-0 training labels and circuit mapping. Timing artifacts are stored
under `validation/baseline-03-timing/seed-*/model.joblib`, separate from
`artifacts/model.joblib`. No validation metric is computed.

The report records training and benchmark counts, input sizes, configuration,
parser latency, every singleton `RuntimeModel.predict` latency, and failures.
The benchmark covers high-qubit broadcasts, both text-size boundaries, a large
comment, static control flow, and every controller-designated benchmark circuit.
Reading/decompression, input construction, model loading, and report writing are
outside parser and prediction timers, matching the official call boundaries.
The parser and each singleton prediction have separate 15-second caps. Repeated
local measurements establish results for these cases and this environment;
they do not certify all possible QASM inputs or validation accuracy.

The recorded step-03 run passed all 21 focused tests, 63 benchmark parser calls,
and 630 singleton predictions, with no measured failures. Maximum latencies were
2.100591 seconds for benchmark parsing, 2.620055 seconds for training-feature
parsing, and 0.163276 seconds for singleton prediction. See the
[evidence summary](validation/baseline-03-summary.md),
[full timing report](validation/baseline-03-timing/report.json), and
[test and integrity checks](validation/baseline-03-checks.json).

For the sampled cut features, `validation/measure_cut_parser.py` accepts the same
`--development-inputs` argument and measures only parsing, using synthetic
fixtures and the packet's designated benchmark circuits. It reads no labels and
fits no models. See the [parser timing report](validation/cut-statistics-timing.json)
and [regression and integrity checks](validation/cut-statistics-checks.json).
Pre-change values for all 90 existing features are retained in
`validation/cut-statistics-before.json` for exact regression comparisons.
