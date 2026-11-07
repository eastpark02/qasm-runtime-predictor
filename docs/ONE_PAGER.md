# Runtime prediction from circuit structure: what we changed and what actually worked

**Result.** Official-metric cross-validation (GroupKFold by circuit, 4 splits): **0.842**, up from **0.806** for our
day-one model and 0.806 for the ExtraTrees branch. Organizers' reference: 0.820 CV / 0.779 hold-out.
On the 283 training circuits with no structural twin in training, the closest thing we have to the hidden
hold-out, we gain **+0.050** over day one (bootstrap CI +0.032 to +0.067).

## The idea in one sentence
The simulator is a matrix-product-state engine in declared qubit order whose cost scales with the bond
dimension it is allowed (the `threshold`), so we **step through every gate in file order** with a cheap model of
that bond dimension, and we **recognise the algorithm family from structure**, which the rules allow.

## Where the gain came from (mean over 4 splits, baseline 0.806)
| step | what it is | gain |
|---|---|---|
| 1. Unconstrain threshold | drop the "runtime must rise with threshold" constraint; the model was being forced to fit a shape the data does not have | +0.013 |
| 2. Moment stepper cost | C++ pass over the expanded gate stream: per-cut bond dimension χ, per-qubit "still classical" flag, cost per 2q gate = D_left × D_right at each threshold | +0.010 |
| 3. Four-model blend | equal-weight log-space mean of 4 gradient-boosted models that differ in features and constraints | +0.005 |
| 4. Structure fingerprints | family detectors (QFT, adder, Sycamore, Grover, BV, arithmetic, layer period) + hashed gate-motif histogram | +0.008 |
| 5. Timeout rule | if any blend member predicts ≥ 60 % of the 6 h cap, predict the cap | +0.002 (57 rows, weak) |

## Which signals actually carried information (Spearman ρ with log runtime)
| signal | ρ @ T=16 | ρ @ T=1024 | note |
|---|---|---|---|
| log gate count (day-one feature) | 0.34 | 0.74 | still the single strongest column; the stepper's value is the *interaction* with it |
| stepper cost at that threshold | 0.37 | 0.62 | beats the old static capacity feature (0.45 → 0.51); the only stepper group that survived ablation |
| fraction of 2q gates at saturated cuts | 0.32 | 0.63 | redundant with cost once both are in the model |
| max bond dimension χ | 0.25 | 0.55 | redundant |
| old static entanglement capacity | 0.45 | 0.51 | order-blind; the stepper replaces it |
| layer repetition period (fingerprint) | – | 0.65 | strongest new column; Sycamore and QAOA-style circuits repeat |
| Sycamore layer period / distinct pairs | – | 0.44 / 0.37 | fires on 226 files, 198 of them 12–53 qubits |
| motif concentration (top-1 share) | – | −0.38 | repetitive circuits are cheap per gate |
| mcphase arithmetic on basis states | – | −0.27 | 47 files; zero realised entanglement despite huge capacity |
| QFT block detector | – | – | 44 files; median log runtime −0.22 vs 0.99 for the rest; adds +0.001 as a feature |

The stepper's clearest win is qualitative: 54 circuits have **zero realised entanglement** (adders on |0⟩, basis-state
QFTs, swap tests, BV oracles). Day one over-predicted them by 0.2–0.3 dex; the stepper brings the bias to ~0.

## Plan vs reality
| planned | outcome |
|---|---|
| cut crossings, entanglement capacity, gate distance, interaction graph (Tier 2 static features) | already in the day-one model; capacity ρ 0.45–0.51; superseded by the stepper cost |
| QFT signature | built, order-agnostic (row- and column-wise, inverse, decimal angles, inside gate bodies); cheap family tag, small gain |
| Sycamore signature | built as layer periodicity; the hand-tuned "flag" was too strict, the raw period is what works |
| monotone constraint in threshold | **hurt** by 0.013; dropped; kept only on one blend member for diversity |
| timeout classifier | replaced by the any-member cap rule; a separate classifier was not needed |
| hyper-parameter sweep | not done; blend diversity gave more than tuning would |
| feature pruning | ablation showed saturation, χ stats, no-op counts and cost quartiles are noise; kept for the blend, prunable for the write-up |
| ensemble of seeds | replaced by a blend of *different* models (+0.005); seeds alone would give ~0 since HGB is deterministic here |
| per-gate trace / breakpoint stepping | built (`--trace`); used to verify the rules on real circuits, not as a feature |
| **not planned: structural twin** (nearest training circuit by motif, size-adjusted runtime) | ρ 0.84–0.95 on its own but 0.29 dex error vs 0.18 for the blend; **lowered CV by 0.009**, switched off |

## Honest limits
Short, heavily entangled circuits (126–2,000 gates, χ 7–49) are still over-predicted by 0.4–0.9 dex: the swap-chain
cost model charges long-range gates too much. Timeout rows score 0.73 on 57 rows. Expect a hold-out drop similar
to the organizers' (−4 pts); our unique-circuit score of 0.763 is the conservative estimate.

**Time guards (15 s per-circuit cap).** Spec-valid files near 256 MB (20M one-line gates, 1.6M gate
definitions, a 100-qubit QFT gate called 25k times, 2,000-operand star gates) used to take 17–150 s.
Now the Python parser stops at 9 s and scales its counts, depth and cut crossings by 1/(fraction of lines
read); the C++ stepper gets `--deadline` and stops at ~11 s (st_ok 0.5, costs scaled by bytes total / bytes
read); the Python fallback checks the clock every 256 gates and cut updates and returns a hint-based
estimate when < 2 s remain. Measured worst case: 11.0 s with the binary, 13.0 s with the binary missing
(load ≈ 3). Files that finish in time give byte-identical features. Extrapolation is crude when the
work is not spread evenly through the file (e.g. all definitions first, one call at the end). Parser slices
start at 16 lines so a tiny file of very expensive lines is still caught, and the cut statistics are
computed per segment, so a 100M-qubit register no longer costs 7 s after the deadline.
