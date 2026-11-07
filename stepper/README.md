# qasm_step

Single-file C++20 tool that parses an OpenQASM 2.0 / 3.0 file, expands every custom
gate down to primitive gates, and steps a simple MPS bond-dimension model gate by gate.
It prints one flat JSON object of `st_*` features (numbers only) as the last stdout line.

## Build

Needs CMake >= 3.16 and any C++20 compiler (Apple clang, gcc, MinGW, MSVC).

macOS / Linux / Git-Bash:

    ./stepper/build.sh            # -> stepper/qasm_step

Windows (PowerShell), with **Visual Studio Build Tools** (the "Desktop development with
C++" workload) and CMake on PATH:

    powershell -ExecutionPolicy Bypass -File stepper\build.ps1   # -> stepper\qasm_step.exe

Windows with **MinGW** (e.g. MSYS2 `mingw-w64-x86_64-gcc` + `cmake`):

    $env:CMAKE_GENERATOR = "MinGW Makefiles"
    powershell -ExecutionPolicy Bypass -File stepper\build.ps1

Both scripts run `cmake -S stepper -B stepper/build -DCMAKE_BUILD_TYPE=Release` and
`cmake --build stepper/build --config Release`, then copy the binary to `stepper/`
(MSVC puts it in `stepper/build/Release/`, the scripts handle both). Delete
`stepper/build/` if you switch generators. No compiler-specific flags are used.

## Usage

    qasm_step [--trace] [--thresholds 16,64,256,512,1024] <path.qasm>

* Success: exit 0, the JSON object is the last stdout line.
* Error (bad args, unreadable file): message on stderr, exit 2.
* `--thresholds`: comma-separated bond-dimension caps T; the per-T keys follow this list.
* `--trace`: before the JSON, one line per primitive gate call:
  `idx name q0 q1 chi_l chi_r dcost_1024 cost_1024`
  (`q1 = -1` for 1q gates, first two operands only for 3q+; `chi_l` / `chi_r` = log2 bond
  dimension of the gate's first cut *before* / *after* the gate (0 0 if nothing entangled);
  `dcost_1024` = cost added by this gate summed over all its cuts; `cost_1024` = running
  total. "1024" means the T=1024 column, or the last threshold if 1024 is not in the list.)

## Model (one line each)

* `chi[k]` = uncapped log2 bond dimension of the cut between declared qubits k and k+1;
  the T-capped value is `min(chi[k], log2 T)`, so one array serves all thresholds.
* Entangling update at cut k: `chi[k] = min(chi[k]+r, chi[k-1]+1, chi[k+1]+1, k+1, n-k-1)`,
  r = 2 for an adjacent swap, else 1. Chi never decreases.
* Non-adjacent 2q gate (a,b): update and charge every cut in [a,b), left to right
  (swap-chain model). 3q+ gates: star of 2q updates from the last operand to each other one.
* Cost of one update at cut k for threshold T: `2^min(chi[k-1],L) * 2^min(chi[k+1],L)`,
  L = log2 T (boundary cuts count as 1).
* Per-qubit flags: `basis` (still a computational basis state) and `entangled` (touched by
  an applied update). Identity-angle gates (|theta mod 2pi| < 1e-9) are skipped first;
  then diagonal gates (cz, cp, crz, rzz, mcphase, ...) entangle only if >= 2 operands are
  non-basis; controlled gates (cx, ccx, mcx, rccx, crx, ch, cu, csx, c3sx, ...) only if a
  control is non-basis; plain `swap` only if an operand is already entangled (it swaps the
  flags); cswap/fredkin only if the control (1st operand) is non-basis (then both targets
  become non-basis; otherwise the targets swap flags, the control is never touched);
  iswap/dcx if any operand is non-basis; rxx/ryy/ecr/xx_plus_yy and unknown
  multi-qubit gates always entangle. measure/reset set the qubit back to basis.
* Custom `gate` definitions shadow builtins; body calls are resolved when the definition
  is parsed (same as the Python parser); a call with the wrong arity is a primitive.
  `if` bodies (v2 inline, v3 blocks incl. one-line `if (c) { x q; }` and `else` blocks) are
  always applied. Statement heads are matched by whole identifier (a gate named
  `measure_x` or `barrier2` is a gate call). The parser is token-driven, so several
  statements per line, or after a `gate ... { }` on the same line, are fine.
* Modifiers `ctrl[(n)] @`, `negctrl @`, `inv @`, `pow(k) @` are stripped; with any
  ctrl/negctrl the gate is applied as one primitive over all operands that always
  entangles (by arity); with only inv/pow the base gate is applied as usual.
  `gphase(...)` is ignored (0 qubits). `for`/`while`/`def`/`box`/... blocks are skipped
  and counted in `st_n_skipped_stmts`.

## Output keys

| key | meaning |
|---|---|
| st_ok | 1 when the binary ran to completion |
| st_n_qubits | declared qubits (all registers) |
| st_n_gates | primitive gate calls after full expansion (no measure/reset/barrier); equals Python `n_gates` |
| st_n_1q / st_n_2q / st_n_3q_plus | primitive gates with 1 / 2 / >= 3 operands; equal Python `n_1q`/`n_2q`/`n_3q_plus` |
| st_n_measure / st_n_reset | measured qubits / reset broadcast steps; equal Python `n_measure` / `n_reset` |
| st_n_entangling_2q | cut updates actually applied to chi (a gate spanning d cuts adds d; a 3q star adds per edge) |
| st_n_noop_2q_classical | multi-qubit gates skipped because their operands are still basis/product states |
| st_n_noop_2q_identity | multi-qubit gates skipped because the angle is 0 mod 2pi |
| st_chi_max | max final chi over cuts (uncapped log2 bond dim) |
| st_chi_mean_final | mean final chi over cuts |
| st_frac_qubits_basis_final | fraction of qubits still in a basis state after the last gate call (trailing measurements ignored) |
| st_n_if | `if` statements seen (v2 inline and v3 blocks) |
| st_truncated | 1 if expansion hit the 2e8 primitive cap |
| st_n_unknown_gates | primitive gates whose name is neither builtin nor defined |
| st_n_skipped_stmts | statements skipped as unsupported (for, while, def, box, ...) |
| st_n_param_parse_err | parameter expressions that failed to parse (treated as non-identity) |
| st_log10_cost_T | log10(1 + total simulated cost) with the bond cap T |
| st_frac_2q_sat_T | fraction of cut updates whose cut was already at the cap (chi >= log2 T) |
| st_cost_frac_q25/q50/q75 | fraction of final cost_1024 accrued at 25/50/75 % of the gate index (cost sampled after every iv-th gate, iv starts at 1 and doubles, dropping every other sample, whenever 256 samples are held; piecewise-linear through (0,0), the samples and (n_gates, final)) |
| st_log10_cost_ep_T | log10(1 + cost) where each 2q edge charges once, at its most expensive cut (MPO-style); chi updates unchanged |
| st_log10_cut_updates | log10(1 + st_n_entangling_2q) |
| st_mean_span_2q | mean (b - a) over applied entangling 2q edges |
| st_n_qft_blocks | QFT / inverse-QFT blocks detected on the expanded gate stream |
| st_qft_max_size | qubits in the largest detected block |
| st_qft_frac_2q | sum of m(m-1)/2 over blocks / st_n_2q (nominal cp count, includes cp(0) terms) |
| st_fam_adder_blocks | Draper-style cp ladders: runs of >= 3 consecutive cp/cphase/cu1 gates on one qubit with angles +-pi/2^e (e >= 0, one sign) whose exponent steps by 1 in one direction over distinct partners, on a qubit that never gets an `h` (QFT rows excluded) |
| st_fam_adder_frac_2q | cp gates inside those ladders / st_n_2q (capped at 1) |
| st_fam_sycamore | 1 when the 2q-layer sequence is periodic (st_fam_sycamore_period > 0), the top-4 patterns cover >= 80 % of 2q gates and there are >= 8 layers (brickwork / Sycamore-style; Trotter brickwork also fires) |
| st_fam_sycamore_pairs | distinct unordered qubit pairs touched by 2q gates (0 if n > 2048) |
| st_fam_sycamore_top4_frac | fraction of 2q gates in layers whose pair set is one of the 4 most common sets; layer = 2q-only ASAP depth (1q gates ignored), pattern = order-free hash of its pairs |
| st_fam_sycamore_period | smallest p in 1..64 with layer pattern[i] == pattern[i-p] for >= 80 % of i (0 = none) |
| st_fam_ghz_chain | cx gates in the longest chain h(q0), cx(q0,q1), cx(q1,q2), ... (target of one = control of the next; any other gate on a chain qubit breaks it) |
| st_fam_ghz_chain_frac | qubits in that chain / st_n_qubits (0 if no cx in it) |
| st_fam_grover_iters | max multiplicity of one identical segment (gate names, qubits, params) between two h-layers (a run of 1q gates with `h` on >= max(2, 3n/4) distinct qubits); 0 if < 2 |
| st_fam_grover_frac | (multiplicity * segment length) / gates observed |
| st_fam_layer_period | most common shift d between a 32-gate window (gate name + absolute qubits, no params) and its previous occurrence; windows sampled 1/4 by content hash; 0 = no repeats |
| st_fam_layer_period_frac | fraction of sampled windows that occurred before (stream coverage by repeats) |
| st_fam_bv_oracle | segments between two h-layers that contain only cx-like 2q gates (>= 2) all with one common target (plus any 1q gates) |
| st_fam_bv_fanin_frac | largest such fan-in / st_n_qubits |
| st_fam_mcphase_arith | 3q+ diagonal or controlled-permutation gates (mcphase, ccz, ccx, mcx, ...) whose controls (all operands but the last) are all basis states (counted over the full stream) |
| st_fam_mcphase_arith_frac | st_fam_mcphase_arith / st_n_3q_plus |
| st_motif_0..63 | fraction of gate 3-grams per hash bucket; token = (class, offset bucket, shares-qubit-with-previous-token), class in {h, x-like, z-like phase, rotation, cx-like, cz/cp-like (incl. rxx/rzz/ecr/unknown 2q), swap-like, 3q+, measure}, offset bucket of q1-q0 in {<=-2, -1, 0 (1q), +1, >=+2}; bucket = top 6 bits of 3gram_key * 0x9e3779b97f4a7c15 (platform-independent) |
| st_motif_entropy | Shannon entropy (bits, max 6) of st_motif_* |
| st_motif_top1_frac | largest st_motif_* |

## QFT detector

Runs on the expanded primitive stream with global qubit indices (so QFTs inside custom
gate bodies count). It records, per qubit pair, the last cp/cphase/cu1 whose angle is
+-pi/2^e (decimal floats included, relative tol 1e-6), and per qubit the last `h`.
On each `h`, it walks a chain of qubits v0, v1, ... (any declared order, ascending,
descending or permuted) where every pair at rank distance d has cp exponent e = d with
one common sign, and every cp lies between the two qubits' h gates. Pairs at distance
> 16 may be missing or cp(0) (approximate QFT). A chain is accepted when it has >= 3
qubits, no other multi-qubit gate (swaps excepted) touched its qubits inside the span,
and the span is not much longer than the block's own gates; growing chains replace the
pending block, so each QFT counts once. Row-wise and column-wise orders, inverse QFT,
trailing swap layer are all handled. Not handled: the semi-classical measured QFT
(`if (c) { p(...) }`), decomposed cp, n > 1024 qubits.
`--trace` prints `qft_block lo hi idx0 idx1 n2q sign` per block (lo/hi = min/max global
qubit, idx = gate indices of the first h and last h, sign = +1 forward / -1 inverse).

## Structure fingerprints

`st_fam_*` and `st_motif_*` are computed on the expanded primitive stream (global qubit
indices, measures as `measure` tokens for the motifs only), O(1) per gate, with our own
64-bit mixing so values are identical across platforms. Only the first 2^24 primitive
gates feed them (mcphase_arith excepted); when a stream is longer, the block counts
(adder_blocks, grover_iters, bv_oracle, and adder_frac_2q's numerator) are scaled by
n_gates / 2^24, the other keys describe the observed prefix. Sycamore layer stats use the
first 2^16 2q layers. The Python fallback (qasm_step_py.py) emits 0 for all of these.
