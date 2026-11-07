param([string]$Zig = '', [switch]$Force)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskManifest = Get-Content -LiteralPath (Join-Path $taskRoot 'runtime-checkpoint.json') -Raw | ConvertFrom-Json
$taskNative = Join-Path $taskRoot 'native'
$taskSource = Join-Path $taskNative 'qasm_inspect.cpp'
$taskUpstream = Join-Path $taskNative 'qasm_step_upstream.cpp'
$taskBaseHash = (Get-FileHash -LiteralPath $taskUpstream -Algorithm SHA256).Hash.ToLowerInvariant()
if ($taskBaseHash -ne $taskManifest.files.'stepper/qasm_step.cpp') { throw 'Inspector base differs from pinned dev source.' }
$taskSourceHash = (Get-FileHash -LiteralPath $taskSource -Algorithm SHA256).Hash.ToLowerInvariant()
$taskRuntime = Join-Path $taskRoot '.local/runtime'
New-Item -ItemType Directory -Path $taskRuntime -Force | Out-Null
$taskExe = Join-Path $taskRuntime 'qasm_inspect.exe'
$taskBuildPath = Join-Path $taskRuntime 'inspector-build.json'
if (-not $Force -and (Test-Path -LiteralPath $taskExe) -and (Test-Path -LiteralPath $taskBuildPath)) {
    $taskPrevious = Get-Content -LiteralPath $taskBuildPath -Raw | ConvertFrom-Json
    if ($taskPrevious.source_sha256 -eq $taskSourceHash -and $taskPrevious.upstream_sha256 -eq $taskBaseHash -and $taskPrevious.binary_sha256 -eq (Get-FileHash -LiteralPath $taskExe -Algorithm SHA256).Hash.ToLowerInvariant()) { Write-Host "Reusing verified inspector: $taskExe"; return }
}
if (-not $Zig) { $Zig = Join-Path $taskRoot '.local/toolchains/zig-x86_64-windows-0.17.1/zig.exe' }
if (-not (Test-Path -LiteralPath $Zig)) { throw 'Run build-stepper.ps1 to provision the pinned Zig compiler first.' }
$env:ZIG_GLOBAL_CACHE_DIR = Join-Path $taskRoot '.local/zig-cache'
& $Zig c++ -std=c++20 -O2 -DNDEBUG -target x86_64-windows-gnu $taskSource -o $taskExe
if ($LASTEXITCODE -ne 0) { throw 'Native inspector compilation failed' }
$taskBuild = [ordered]@{source_sha256=$taskSourceHash; upstream_sha256=$taskBaseHash; binary_sha256=(Get-FileHash -LiteralPath $taskExe -Algorithm SHA256).Hash.ToLowerInvariant(); compiler=(& $Zig version); commit=$taskManifest.commit}
[IO.File]::WriteAllText($taskBuildPath, ($taskBuild | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
Write-Host "Built app-owned native inspector: $taskExe"
