param([Parameter(Mandatory=$true)][string]$Source, [Parameter(Mandatory=$true)][string]$Destination)
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
foreach ($item in Get-ChildItem -LiteralPath $Source -Force) {
    if ($item.PSIsContainer) {
        $childDestination = Join-Path $Destination $item.Name
        New-Item -ItemType Directory -Path $childDestination -Force | Out-Null
        Copy-Item -Path (Join-Path $item.FullName '*') -Destination $childDestination -Recurse -Force
    } else {
        Copy-Item -LiteralPath $item.FullName -Destination (Join-Path $Destination $item.Name) -Force
    }
}
