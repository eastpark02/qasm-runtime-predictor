# Build qasm_step on Windows (Visual Studio Build Tools or MinGW + CMake).
# Usage:  powershell -ExecutionPolicy Bypass -File stepper\build.ps1
#   MinGW: set $env:CMAKE_GENERATOR = "MinGW Makefiles" before running.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
cmake -S $here -B "$here\build" -DCMAKE_BUILD_TYPE=Release
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
cmake --build "$here\build" --config Release
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$cands = @("$here\build\Release\qasm_step.exe", "$here\build\qasm_step.exe",
           "$here\build\qasm_step")
foreach ($c in $cands) {
  if (Test-Path $c) {
    if ($c.EndsWith(".exe")) { $dst = "$here\qasm_step.exe" } else { $dst = "$here\qasm_step" }
    Copy-Item -Force $c $dst
    Write-Host "built: $c -> $dst"
    exit 0
  }
}
Write-Error "build finished but binary not found"
exit 1
