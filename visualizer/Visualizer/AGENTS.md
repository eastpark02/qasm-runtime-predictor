# Canonical Visualizer

This directory (qasm-runtime-predictor/visualizer/Visualizer) is the user's
authoritative local Visualizer. Apply all Visualizer changes directly here.
Do not edit or launch the obsolete sibling QuantumRings/Visualizer app.
Preserve the current repository's large-circuit/windowed-inspection behavior
when porting features from older copies. Never replace this app wholesale with
an older staging snapshot. The running /api/health application_root must point
here. Predictor defaults resolve to this enclosing qasm-runtime-predictor repo.
Do not change model artifacts, training, audit data, or prediction provenance
while working on this app. Run focused tests for the authorized app changes.
