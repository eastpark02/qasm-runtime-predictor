#!/usr/bin/env bash
# Build qasm_step (macOS / Linux / MinGW-bash). Run from anywhere.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cmake -S "$HERE" -B "$HERE/build" -DCMAKE_BUILD_TYPE=Release
cmake --build "$HERE/build" --config Release
for cand in "$HERE/build/qasm_step" "$HERE/build/Release/qasm_step" \
            "$HERE/build/qasm_step.exe" "$HERE/build/Release/qasm_step.exe"; do
  if [ -f "$cand" ]; then
    case "$cand" in
      *.exe) rm -f "$HERE/qasm_step.exe"; cp "$cand" "$HERE/qasm_step.exe" ;;
      *)     rm -f "$HERE/qasm_step"; cp "$cand" "$HERE/qasm_step" ;;
    esac
    echo "built: $cand -> $HERE/$(basename "$cand")"
    exit 0
  fi
done
echo "build finished but binary not found" >&2
exit 1
