param([string]$Python = '', [switch]$SkipDevelopmentAllowlist, [switch]$BackendOnly)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        if ($Python) { & uv venv --python $Python .venv } else { & uv venv --python 3.12 .venv }
    } elseif ($Python) {
        & $Python -m venv .venv
    } else {
        & py -3.12 -m venv .venv
    }
    if ($LASTEXITCODE -ne 0) { throw 'Could not create a Python 3.12 virtual environment. Pass -Python with its full executable path.' }
}
$venvPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (Get-Command uv -ErrorAction SilentlyContinue) {
    if (Test-Path -LiteralPath 'requirements.lock.txt') {
        & uv pip install --python $venvPython -r requirements.lock.txt
        if ($LASTEXITCODE -ne 0) { throw 'Pinned Python dependency installation failed' }
    }
    & uv pip install --python $venvPython -e '.[test]'
} else {
    & $venvPython -m ensurepip --upgrade
    if (Test-Path -LiteralPath 'requirements.lock.txt') {
        & $venvPython -m pip install -r requirements.lock.txt
        if ($LASTEXITCODE -ne 0) { throw 'Pinned Python dependency installation failed' }
    }
    & $venvPython -m pip install -e '.[test]'
}
if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed' }
Write-Host 'Existing development/synthetic allowlist preserved. Setup never approves new inputs.'
& (Join-Path $PSScriptRoot 'scripts/build-stepper.ps1')
& (Join-Path $PSScriptRoot 'scripts/build-inspector.ps1')
if ($BackendOnly) { Write-Host 'Backend setup complete. Run .\start.ps1 -NoBrowser'; exit 0 }
Push-Location -LiteralPath (Join-Path $PSScriptRoot 'frontend')
try {
    if (Test-Path -LiteralPath 'package-lock.json') { & npm.cmd ci } else { & npm.cmd install }
    if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed' }
    & npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed' }
} finally { Pop-Location }
Write-Host 'Setup complete. Run .\start.ps1 to open the explorer.'
