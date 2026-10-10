param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExecutable
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Runner = Join-Path $ProjectRoot "scripts\run_email_order_intake.py"
$LogDir = Join-Path $ProjectRoot "logs"
$LogFile = Join-Path $LogDir "email_order_intake_scheduler.log"

if (-not (Test-Path -LiteralPath $Runner)) {
    throw "Email intake runner not found: $Runner"
}
if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
    throw "Pinned Python interpreter not found: $PythonExecutable"
}
$ResolvedPython = (Resolve-Path -LiteralPath $PythonExecutable).Path

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Set-Location -LiteralPath $ProjectRoot

$startedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
try {
    $output = & $ResolvedPython -X utf8 $Runner 2>&1
    $exitCode = $LASTEXITCODE
    $line = ($output | Out-String).Trim()
    Add-Content -LiteralPath $LogFile -Encoding UTF8 -Value "[$startedAt] exit=$exitCode $line"
    if ($exitCode -eq 3) {
        # Another poll is already running. The database lock is the source of truth;
        # skipping this tick is safe and avoids overlapping mailbox downloads.
        exit 0
    }
    exit $exitCode
} catch {
    Add-Content -LiteralPath $LogFile -Encoding UTF8 -Value "[$startedAt] exit=1 scheduler_error=$($_.Exception.Message)"
    exit 1
}
