Cycle-0002 candidate timing completed with exit code 0 and no measurement failures. All 26 local tests passed.

Both seeds fitted every one of the 1,692 supplied fold-0 rows from 340 circuits. Loaded artifacts verified 400 absolute_error trees, n_jobs=4, 92 features, and 1,692 rows at every tree root. No benchmark rows were added to training.

Seed 42 fit/load: 38.676214/0.355374 seconds. Seed 43 fit/load: 36.127211/0.433453 seconds. Total measurement: 230.438642 seconds, completed before the supplied deadline.

All 13 synthetic cases and eight designated circuits used three repeats and all five training thresholds, yielding 63 parser calls and 630 singleton predictions across both seeds. No warmups were discarded; all predictions were finite and bounded, with none missing. Maximum benchmark parser/training parser/singleton prediction times: 2.294342/1.445679/0.225019 seconds, below their separate 15-second caps.

The command log preserves 17446 scikit-learn parallel-configuration warnings. These were warnings rather than failed calls, and were not suppressed. See checks.json for details.

Source and input hashes remained stable. Existing tracked files, including historical baseline evidence, are unchanged. Models here are timing-only artifacts, separate from artifacts/model.joblib. Only the designated public packet labels were used; no accuracy evaluation or audit access occurred. These local measurements provide neither validation improvement evidence nor a universal latency guarantee.

Evidence: report.json, command.json, command.log, checks.json, artifact-sha256.json, seed-42/model.joblib, and seed-43/model.joblib.
