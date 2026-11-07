# Baseline step 03: documentation and latency evidence

Run `iqc-2026-runtime`, cycle `cycle-0001`, step `step-02-attempt-0-code`. Measurement finished 2026-11-07T06:18:10.355069+00:00.

All 21 focused tests passed. All 340 training-feature parses, 63 benchmark parser calls, and 630/630 singleton predictions passed their checks; there were no missing predictions or measured failures. Each timed parser/prediction call stayed within its separate 15-second cap.

Maximum benchmark parser latency: 2.100591 s. Maximum training-feature parser latency: 2.620055 s. Maximum singleton prediction latency: 0.163276 s.

Two timing-only ExtraTrees models used 400 trees, min_samples_leaf=2, max_features=1.0, n_jobs=4, and seeds 42/43. Each fit used all 1,692 supplied fold-0 training rows from 340 circuits, with 90 structure features plus threshold and log2_threshold. DictVectorizer and median imputation were fit within those training calls. Synthetic cases were never used for fitting.

Each of the 21 benchmark cases was parsed three times and predicted once per seed and threshold (16, 64, 256, 512, 1024) per repeat. There were no discarded warmup calls. Timers surround only RuntimeModel.featurize(text) or one RuntimeModel.predict(features, threshold), including adapter and estimator overhead. Reads/decompression, input construction, model fitting/loading, checks, and evidence writing are excluded. Training-feature parser calls are reported separately.

Times below are maxima over the recorded calls. n_ops is the parser histogram value, which may be sampled or broadcast-uncorrected; it is not an executed-operation count. Full features, sample fractions, per-call latencies and predictions, input file sizes, environment versions, artifact hashes, and failure accounting are in the JSON report.

| Case | Text chars | Comment-stripped chars | Qubits | n_ops | Max parser s | Max singleton s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| builtin-broadcast-25000 | 51 | 51 | 50000 | 25000 | 0.227023 | 0.130591 |
| builtin-broadcast-25001 | 51 | 51 | 50002 | 1 | 0.141613 | 0.103085 |
| builtin-broadcast-100000 | 53 | 53 | 200000 | 1 | 0.215603 | 0.099470 |
| custom-broadcast-100000 | 86 | 86 | 200000 | 2 | 0.316933 | 0.153478 |
| isolated-qubits-100000 | 37 | 37 | 100000 | 1 | 0.044824 | 0.120713 |
| dependency-chars-1499999 | 1,499,999 | 1,499,999 | 2 | 23076 | 0.344073 | 0.105814 |
| dependency-chars-1500000 | 1,500,000 | 1,500,000 | 2 | 23076 | 0.368910 | 0.103079 |
| dependency-chars-1500001 | 1,500,001 | 1,500,001 | 2 | 23076 | 0.358690 | 0.113155 |
| histogram-chars-7999999 | 7,999,999 | 7,999,999 | 3 | 999990 | 1.214338 | 0.110370 |
| histogram-chars-8000000 | 8,000,000 | 8,000,000 | 3 | 999990 | 1.026862 | 0.113341 |
| histogram-chars-8000001 | 8,000,001 | 8,000,001 | 3 | 499989.333 | 0.567684 | 0.140402 |
| large-comment-small-stripped-body | 8,000,037 | 34 | 2 | 1 | 0.135623 | 0.163276 |
| static-branches-and-loop | 109 | 109 | 2 | 3 | 0.000332 | 0.097109 |
| 27dbf314.qasm | 27,979,254 | 27,979,254 | 56 | 2020208.01 | 0.644301 | 0.086763 |
| 2a04dd73.qasm | 25,923,623 | 25,923,623 | 62 | 1772773.24 | 0.987908 | 0.109459 |
| 24ca3082.qasm | 218,358,866 | 218,358,866 | 256 | 5742992.52 | 2.100591 | 0.112149 |
| 12157215.qasm | 16,419,384 | 16,419,384 | 54 | 1159494.22 | 0.800636 | 0.074528 |
| c6678d16.qasm | 6,867,936 | 6,867,936 | 42 | 454543 | 0.796491 | 0.113333 |
| 8f8fbcbd.qasm | 6,209,274 | 6,209,274 | 58 | 437338 | 1.099565 | 0.113502 |
| 0534bf07.qasm | 51,670,933 | 51,670,933 | 256 | 2287937.05 | 1.046198 | 0.156674 |
| 46073cb3.qasm | 27,308,605 | 27,308,605 | 128 | 722659.051 | 0.806631 | 0.114911 |

The 25,001-wide broadcast retained gate_cx=1 and expanded_counts_complete=1 while dependency coverage was incomplete. Crossing 8,000,000 comment-stripped characters changed the histogram branch. The 8,000,037-character comment case stripped to 34 characters and used the smaller branch. The static control-flow case counted three written gates without executing its million-iteration loop. These observations support the corrected approximation descriptions.

Parser executable AST is unchanged after excluding docstrings. Model code, configuration, public adapters, existing tests, official run.py and score.py, I/O code, CLI, and dependency files match the starting worktree. No audit labels, private protocol manifest, controller records, or other label files were opened; no validation scores were computed. Results apply to the measured local cases and do not establish universal timing guarantees or validation improvement.

Evidence: [test and integrity checks](baseline-03-checks.json), [measurement script](measure_baseline.py), [full timing report](baseline-03-timing/report.json), [command and exit status](baseline-03-timing/command.json), [complete measurement log](baseline-03-timing/command.log), [seed 42 timing model](baseline-03-timing/seed-42/model.joblib), [seed 43 timing model](baseline-03-timing/seed-43/model.joblib). Timing models are separate from artifacts/model.joblib.

The focused unittest command, script compilation, measurement command, and whitespace checks exited 0. Git emitted only its existing inaccessible user-ignore-file and LF-to-CRLF warnings; no Git configuration was changed.
