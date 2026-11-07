# Repository migration

The runtime pipeline was imported on 2026-11-07T01:26:12.164494-07:00 into branch `Aiden` of `eastpark02/qasm-runtime-predictor`.

Source checkout: `C:\Users\apier\OneDrive\Documents\Code\QuantumRings\ml-agent-pipeline`. Source stable commit: `239358ecd0ceefe94edf65e648277747c61675ef`.
The destination repository's original history, LICENSE, and main/dev branches are preserved. Pipeline history is joined by a merge commit; the original README is in this directory.

All pipeline payload files, including trained artifacts, validation evidence, and experimental code snapshots, were copied and hash-checked locally. Existing ignored outputs remain ignored by Git. Nested experiment `.git` pointers, Python bytecode, generated egg-info, and environments were excluded. Copied experiment folders are snapshots, not live Git worktrees. Unpromoted experimental code is not the validated default model.

The `ML-Agent` controller and run history remain in the sibling QuantumRings directory. Its existing run has recorded paths into the old checkout and was asked to pause for migration; do not assume those paths were rewritten. Its configured pipeline is still the old checkout until an explicit run relocation is completed. Do not resume it against this repo without reconciling those worktrees and the recorded stable commit.

Recovery copies: `C:\Users\apier\OneDrive\Documents\ChatGPT\Quantum\.mas-build\repository-migration-2026-11-07` contains verified Git bundles, a full pipeline payload archive, the prior VS Code workspace configuration, and a hash manifest. The source checkout was retained as an additional recovery copy.

This migration does not open audit labels, train a replacement model, or change evaluation results. The included `artifacts/model.joblib` retains development-only provenance.
