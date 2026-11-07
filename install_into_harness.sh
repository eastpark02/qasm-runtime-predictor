#!/usr/bin/env bash
# Copy our parser + model into the organizer's submission harness.
# Usage: ./install_into_harness.sh [path/to/submission-harness]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="${1:-$HERE/../quantumrings-challenge-iqc-2026/submission-harness}"
mkdir -p "$DEST/artifacts"
cp "$HERE/submission/model.py" "$DEST/model.py"
cp "$HERE/qasm_features.py" "$DEST/qasm_features.py"
cp "$HERE/artifacts/model.pkl" "$DEST/artifacts/model.pkl"
echo "installed into $DEST"
