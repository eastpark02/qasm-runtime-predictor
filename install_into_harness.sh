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
cp "$HERE/qasm_step.py" "$DEST/qasm_step.py"
cp "$HERE/qasm_step_py.py" "$DEST/qasm_step_py.py"
BIN=""
for c in "$HERE/stepper/qasm_step.exe" "$HERE/stepper/qasm_step" \
         "$HERE/stepper/build/Release/qasm_step.exe" "$HERE/stepper/build/Release/qasm_step" \
         "$HERE/stepper/build/qasm_step.exe" "$HERE/stepper/build/qasm_step"; do
  if [ -f "$c" ]; then BIN="$c"; break; fi
done
if [ -n "$BIN" ]; then
  # rm first: overwriting a signed binary in place on macOS gets it SIGKILLed (exit 137)
  rm -f "$DEST/$(basename "$BIN")"
  cp "$BIN" "$DEST/$(basename "$BIN")"
  chmod +x "$DEST/$(basename "$BIN")"
  echo "copied stepper binary $BIN"
else
  echo "WARNING: no stepper binary found under $HERE/stepper; run stepper/build.sh (or build.ps1)." >&2
  echo "WARNING: RuntimeModel() will refuse to load until the binary is installed." >&2
fi
echo "installed into $DEST"
