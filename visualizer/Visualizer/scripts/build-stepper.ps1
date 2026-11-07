param(
    [string]$Predictor = (Join-Path $PSScriptRoot '../../quantum-runtime-prediction-model/qasm-runtime-predictor'),
    [string]$Zig = '',
    [switch]$Force
)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskManifest = Get-Content -LiteralPath (Join-Path $taskRoot 'runtime-checkpoint.json') -Raw | ConvertFrom-Json
$taskSource = Join-Path $Predictor 'stepper/qasm_step.cpp'
$taskSourceHash = (Get-FileHash -LiteralPath $taskSource -Algorithm SHA256).Hash.ToLowerInvariant()
if ($taskSourceHash -ne $taskManifest.files.'stepper/qasm_step.cpp') { throw 'Stepper source differs from the pinned dev revision; review the checkpoint manifest first.' }
$taskRuntime = Join-Path $taskRoot '.local/runtime'
New-Item -ItemType Directory -Path $taskRuntime -Force | Out-Null
$taskExe = Join-Path $taskRuntime 'qasm_step.exe'
$taskBuildPath = Join-Path $taskRuntime 'build.json'
if (-not $Force -and (Test-Path -LiteralPath $taskExe) -and (Test-Path -LiteralPath $taskBuildPath)) {
    $taskPrevious = Get-Content -LiteralPath $taskBuildPath -Raw | ConvertFrom-Json
    if ($taskPrevious.source_sha256 -eq $taskSourceHash -and $taskPrevious.binary_sha256 -eq (Get-FileHash -LiteralPath $taskExe -Algorithm SHA256).Hash.ToLowerInvariant()) {
        Write-Host "Reusing verified native C++ stepper: $taskExe"
        return
    }
}
if (-not $Zig) {
    $taskTools = Join-Path $taskRoot '.local/toolchains'
    $Zig = Join-Path $taskTools 'zig-x86_64-windows-0.17.1/zig.exe'
    if (-not (Test-Path -LiteralPath $Zig)) {
        New-Item -ItemType Directory -Path $taskTools -Force | Out-Null
        $taskArchive = Join-Path $taskTools 'zig-0.17.1.zip'
        $taskArchiveHash = 'b03f477a1943673e02aca0f570e8030dffd536ce1f45cbc4d3326ec31159562e'
        if (-not (Test-Path -LiteralPath $taskArchive) -or (Get-FileHash -LiteralPath $taskArchive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $taskArchiveHash) {
            Invoke-WebRequest -Uri 'https://ziglang.org/download/0.17.1/zig-x86_64-windows-0.17.1.zip' -OutFile $taskArchive
        }
        if ((Get-FileHash -LiteralPath $taskArchive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $taskArchiveHash) { throw 'Compiler archive checksum mismatch' }
        Expand-Archive -LiteralPath $taskArchive -DestinationPath $taskTools -Force
    }
}
$env:ZIG_GLOBAL_CACHE_DIR = Join-Path $taskRoot '.local/zig-cache'
& $Zig c++ -std=c++20 -O2 -DNDEBUG -target x86_64-windows-gnu $taskSource -o $taskExe
if ($LASTEXITCODE -ne 0) { throw 'C++ stepper compilation failed' }
$taskBuild = [ordered]@{source_sha256=$taskSourceHash; binary_sha256=(Get-FileHash -LiteralPath $taskExe -Algorithm SHA256).Hash.ToLowerInvariant(); compiler=(& $Zig version); source=$taskSource}
[IO.File]::WriteAllText((Join-Path $taskRuntime 'build.json'), ($taskBuild | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
Write-Host "Built native C++ stepper: $taskExe"
Write-Host 'Upstream source and model artifacts were not modified.'
