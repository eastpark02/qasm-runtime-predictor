param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$venvPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $venvPython) -or -not (Test-Path -LiteralPath 'frontend/dist/index.html')) {
    throw 'Run .\setup.ps1 first.'
}
$env:PYTHONPATH = Join-Path $PSScriptRoot 'backend'
if (-not $NoBrowser) {
    $launcher = Join-Path $PSScriptRoot 'scripts/open-browser.ps1'
    Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $launcher + '"')) -WindowStyle Hidden | Out-Null
}
Write-Host 'Quantum Circuit Explorer: http://127.0.0.1:8000'
Write-Host 'Loopback only. Stop with Ctrl+C. Restart explicitly to load a newer model checkpoint.'
& $venvPython -m uvicorn visualizer.app:app --host 127.0.0.1 --port 8000
