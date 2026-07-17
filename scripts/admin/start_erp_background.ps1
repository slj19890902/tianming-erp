$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$launcher = Join-Path $projectRoot "scripts\windows\start_erp.ps1"

if (-not (Test-Path -LiteralPath $launcher)) {
    Write-Error "Hardened ERP launcher not found: $launcher"
    exit 1
}

& $launcher -NoBrowser
exit $LASTEXITCODE
