# Experiment log: moment stepper, structure fingerprints, blend (2026-11-07)

All scores are the official challenge metric (`submission-harness/score.py` semantics:
timeouts count as 21,600 s, predictions capped at the cap only on timeout rows), cross-validated with
GroupKFold(5) by filename. "det" is the deterministic split; "mean4" averages det plus three shuffled
splits (seeds 0/1/2). Split-to-split noise is about 0.004.

## Baseline
| model | det | mean4 |
|---|---|---|
| shipped baseline (43 static features, HGB, monotone in threshold) | 0.8046 | 0.8062 |
| same features, no monotone constraint + unconstrained threshold copy ("base_free") | 0.8193 | 0.8191 |

The monotone constraint on threshold cost about 0.013 on its own.

## Stage 1: MPS moment stepper (`stepper/qasm_step.cpp`)
Gate-by-gate pass in file order over the fully expanded primitive stream. Per cut k an uncapped
log2 bond dimension chi[k] with `chi[a] = min(chi[a]+r, chi[a-1]+1, chi[a+1]+1, a+1, n-a-1)` on each
entangling 2q update (r=2 swap, else 1); long-range gates update every cut they cross; per-qubit
"still in a computational basis state" flags make diagonal gates on basis qubits free. Cost per update
and threshold T: `2^min(chi[a-1],log2 T) * 2^min(chi[a+1],log2 T)`.
- Counts match the qiskit-verified Python parser on all 532 files; 218 MB file in about 1 s.
- Signal: Spearman of log cost vs log runtime 0.62 at T>=256 (old static capacity feature: 0.51).
- CV: +0.005 to +0.008 over base_free, CI touching zero. Only the cost columns carried signal;
  saturation fraction, chi stats, no-op counts, cost quartiles were noise.
- Fixes the zero-realized-entanglement circuits (base_free over-predicted them by 0.2-0.3 dex);
  over-predicts short circuits with high chi.

## Stage 2: endpoint cost, order-agnostic QFT detector
- Endpoint-only charge (one charge per long-range gate at its most expensive cut) ties the per-cut cost.
- QFT detector finds 44 files (26 single, 18 forward+inverse pairs), both row-wise and column-wise
  ladders, decimal angles, inside custom gate bodies. Adds about +0.001 as a feature.
- Single stepper models: +0.010 over base_free, CI excludes zero (mean4 0.829).
- Equal-weight log-space blend of 4 HGB members (base_free, st_none, st_costsel, st_norm): **0.8337 mean4**,
  +0.0146 over base_free, CI [+0.007, +0.022]. Weight tuning gains 0.0002; isotonic projection +0.0003.

## Stage 3: structure fingerprints
Family detectors (adder ladders, Sycamore layer periodicity, GHZ chains, Grover repeats, BV fan-in,
mcphase arithmetic, generic layer period) plus a 64-bucket hashed 3-gram motif histogram over
(gate class, qubit-offset pattern). Strongest single columns: layer period (Spearman 0.65 with log runtime),
Sycamore period, motif concentration.
- Blend with fingerprints: **0.8401 det, 0.8417 mean4** (members 0.834 each).

### Does the gain generalise beyond near-duplicate families? (independent check by the review session)
Twins defined as circuits sharing exact (version, n_qubits, n_gates, n_2q, depth): 249 circuits in 65 families, 283 unique.
On the det-split out-of-fold predictions, official metric:

| | all | twin circuits | unique circuits | v2 | v3 | timeout rows |
|---|---|---|---|---|---|---|
| shipped blend | 0.8401 | 0.9267 | 0.7633 | 0.8626 | 0.7958 | 0.7334 |
| old baseline | 0.8047 | 0.9074 | 0.7135 | 0.8375 | 0.7401 | 0.7264 |

On the 283 unique circuits the blend is +0.050 over the baseline (cluster-bootstrap CI [+0.032, +0.067]) and
+0.032 over base_free (CI [+0.015, +0.050]). The gain is not a duplicate-family artifact, which matters for
the hidden holdout.

## Tried and rejected
- **Structural twin feature** (nearest training circuit by motif cosine, size-adjusted runtime, fold-safe):
  Spearman 0.84-0.95 alone, but MAE 0.29 dex vs 0.18 for the blend; adding it lowers every member by
  about 0.016 and the blend by 0.009 on every split. Not shipped (`train.py --no-twin`).
- Cost normalisation by gate count: +0.001, kept only as a blend member for diversity.
- Monotone constraint on `cost_sel`: no gain as a single model; kept as one blend member.

## Timeout rule (in `submission/model.py`)
If any blend member predicts >= 60% of the cap, predict the cap. On out-of-fold predictions this lifts
the 57 timeout rows from 0.708 to 0.813 and costs the other 2,591 rows 0.0003 (net +0.002; selected on
57 rows, so treat as weak).

## Shipped
`artifacts/model.pkl` = blend4 without twin. Harness dry run on all 532 compressed train circuits:
2,660 rows, no crashes, worst parse 7.0 s, worst predict 2.4 s (under CPU contention). Before the
holdout run: `python stepper/survey_tokens.py ../holdout-circuits` must print "OK: no unknown tokens".
